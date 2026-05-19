const { regClass, property } = Laya;

type CaptureView = {
    id?: string;
    view_id?: string;
    yaw: number;
    pitch?: number;
    file_name?: string;
};

type CaptureCommand = {
    enabled?: boolean;
    nonce: string;
    server_base_url?: string;
    post_url?: string;
    camera_name?: string;
    target_name?: string;
    width?: number;
    height?: number;
    center?: number[];
    target_size?: number[];
    distance_scale?: number;
    min_distance?: number;
    fov?: number;
    capture_mode?: "auto" | "orbit_camera" | "rotate_target";
    yaw_offset?: number;
    pitch_offset?: number;
    target_yaw_sign?: number;
    target_pitch_sign?: number;
    target_base_yaw?: number;
    target_base_pitch?: number;
    transparent_background?: boolean;
    zero_transparent_rgb?: boolean;
    alpha_from_rgb?: boolean;
    alpha_from_rgb_threshold?: number;
    alpha_source?: "silhouette_mask" | "alpha_from_rgb" | "render_alpha";
    mask_alpha_mode?: "binary" | "soft";
    mask_alpha_threshold?: number;
    flip_y?: boolean;
    render_texture_srgb?: boolean;
    material_patch?: MaterialPatch;
    views?: CaptureView[];
};

type MaterialPatch = {
    target_name?: string;
    values?: { [name: string]: number | number[] | boolean };
};

type MaterialPatchResult = {
    applied: boolean;
    materialCount: number;
    valueCount: number;
    error?: string;
};

type RendererState = {
    source: any;
    enabled: boolean | null;
    materials: Laya.Material[] | null;
};

@regClass()
export class MaterialFitCapture extends Laya.Script3D {
    @property({ type: String, caption: "Server Base URL" })
    public serverBaseUrl: string = "http://127.0.0.1:8787";

    @property({ type: String, caption: "Default Camera Name" })
    public cameraName: string = "";

    @property({ type: String, caption: "Default Target Name" })
    public targetName: string = "";

    @property({ type: Number, caption: "Poll Interval Ms" })
    public pollIntervalMs: number = 500;

    @property({ type: Boolean, caption: "Auto Poll" })
    public autoPoll: boolean = true;

    private _busy: boolean = false;
    private _lastNonce: string = "";
    private _nextPollAt: number = 0;
    private _pollFailureCount: number = 0;

    public onEnable(): void {
        (Laya.Browser.window as any).__materialFitCapture = (command: CaptureCommand) => this.capture(command);
        if (this.autoPoll) {
            Laya.timer.loop(Math.max(100, this.pollIntervalMs), this, this.pollCommand);
            Laya.timer.once(100, this, this.pollCommand);
        }
    }

    public onDisable(): void {
        Laya.timer.clear(this, this.pollCommand);
    }

    private async pollCommand(): Promise<void> {
        if (this._busy) {
            return;
        }
        if (Date.now() < this._nextPollAt) {
            return;
        }
        try {
            const url = `${this.serverBaseUrl}/material-fit/capture-command?last_nonce=${encodeURIComponent(this._lastNonce)}`;
            const response = await fetch(url);
            if (!response.ok) {
                this.schedulePollRetry();
                return;
            }
            this._pollFailureCount = 0;
            this._nextPollAt = 0;
            const command = await response.json() as CaptureCommand;
            if (!command || command.enabled === false || !command.nonce || command.nonce === this._lastNonce) {
                return;
            }
            this._lastNonce = command.nonce;
            await this.capture(command);
        } catch (error) {
            this.schedulePollRetry();
        }
    }

    private schedulePollRetry(): void {
        this._pollFailureCount = Math.min(this._pollFailureCount + 1, 6);
        const delay = Math.min(10000, 500 * Math.pow(2, this._pollFailureCount));
        this._nextPollAt = Date.now() + delay;
    }

    private async capture(command: CaptureCommand): Promise<void> {
        if (this._busy) {
            return;
        }
        this._busy = true;
        if (command.nonce) {
            this._lastNonce = command.nonce;
        }
        const startedAt = Date.now();
        try {
            const width = Math.max(1, Math.floor(command.width || 900));
            const height = Math.max(1, Math.floor(command.height || 700));
            const camera = this.resolveCamera(command.camera_name || this.cameraName);
            const target = this.resolveTarget(command.target_name || this.targetName);
            if (!camera) {
                throw new Error(`Camera not found: ${command.camera_name || this.cameraName || "(owner/default)"}`);
            }
            if (!target && !command.center) {
                throw new Error(`Target not found and command.center missing: ${command.target_name || this.targetName || "(empty)"}`);
            }

            const center = this.resolveCenter(command, target);
            const radius = this.resolveRadius(command, target);
            const captureMode = this.resolveCaptureMode(command, camera, target);
            const originalTargetEuler = target ? target.transform.localRotationEuler.clone() : null;
            const previousTarget = camera.renderTarget;
            const previousFov = camera.fieldOfView;
            const previousClearColor = camera.clearColor ? camera.clearColor.clone() : null;
            const renderTexture = new Laya.RenderTexture(
                width,
                height,
                Laya.RenderTargetFormat.R8G8B8A8,
                Laya.RenderTargetFormat.DEPTH_16,
                false,
                1,
                false,
                command.render_texture_srgb !== false,
            );
            camera.renderTarget = renderTexture;
            if (typeof command.fov === "number" && command.fov > 0) {
                camera.fieldOfView = command.fov;
            }
            if (command.transparent_background !== false) {
                camera.clearColor = new Laya.Color(0, 0, 0, this.resolveAlphaSource(command) === "render_alpha" ? 0 : 1);
            }

            const patchResult = this.applyMaterialPatch(command, target);
            await this.postLog(
                "material_patch",
                `applied=${patchResult.applied} materials=${patchResult.materialCount} values=${patchResult.valueCount}${patchResult.error ? ` error=${patchResult.error}` : ""}`,
            );

            const views = command.views && command.views.length > 0
                ? command.views
                : [{ yaw: 0, pitch: 0, file_name: "laya_capture.png" }];

            try {
                for (let index = 0; index < views.length; index++) {
                    const view = views[index];
                    if (captureMode === "rotate_target") {
                        if (!target || !originalTargetEuler) {
                            throw new Error("rotate_target mode requires target_name");
                        }
                        this.rotateTargetForView(target, originalTargetEuler, view, command);
                    } else {
                        this.placeCamera(camera, center, radius, view, command);
                    }
                    await this.waitFrames(2);
                    const pixels = await this.readPixels(renderTexture, width, height);
                    const alphaSource = this.resolveAlphaSource(command);
                    if (command.transparent_background !== false && alphaSource === "silhouette_mask" && target) {
                        const maskPixels = await this.renderSilhouetteMask(camera, renderTexture, target, width, height);
                        this.applyMaskAlpha(pixels, maskPixels, command.mask_alpha_mode, command.mask_alpha_threshold);
                    } else if (command.transparent_background !== false && alphaSource === "alpha_from_rgb") {
                        this.liftRgbIntoAlpha(pixels, command.alpha_from_rgb_threshold);
                    }
                    if (command.zero_transparent_rgb !== false) {
                        this.zeroTransparentRgb(pixels);
                    }
                    const dataUrl = this.pixelsToPngDataUrl(pixels, width, height, command.flip_y === true);
                    await this.postImage(command, view, index, dataUrl, width, height, patchResult);
                }
            } finally {
                if (target && originalTargetEuler) {
                    target.transform.localRotationEuler = originalTargetEuler;
                }
                camera.renderTarget = previousTarget;
                camera.fieldOfView = previousFov;
                if (previousClearColor) {
                    camera.clearColor = previousClearColor;
                }
                renderTexture.destroy();
            }

            await this.postLog("completed", `Captured ${views.length} views in ${Date.now() - startedAt}ms`);
        } catch (error) {
            await this.postLog("capture_error", String(error));
        } finally {
            this._busy = false;
        }
    }

    private resolveCamera(name: string): Laya.Camera | null {
        if (this.owner instanceof Laya.Camera) {
            return this.owner as Laya.Camera;
        }
        const root = this.sceneRoot();
        const node = name ? this.findNodeByName(root, name) : this.findFirstCamera(root);
        return node instanceof Laya.Camera ? node as Laya.Camera : null;
    }

    private resolveTarget(name: string): Laya.Sprite3D | null {
        if (!name) {
            return null;
        }
        const node = this.findNodeByName(this.sceneRoot(), name);
        return node instanceof Laya.Sprite3D ? node as Laya.Sprite3D : null;
    }

    private sceneRoot(): any {
        let node: any = this.owner;
        while (node && node.parent) {
            node = node.parent;
        }
        return node || this.owner;
    }

    private findNodeByName(root: any, name: string): any {
        if (!root || !name) {
            return null;
        }
        if (root.name === name) {
            return root;
        }
        const count = typeof root.numChildren === "number" ? root.numChildren : 0;
        for (let i = 0; i < count; i++) {
            const found = this.findNodeByName(root.getChildAt(i), name);
            if (found) {
                return found;
            }
        }
        return null;
    }

    private findFirstCamera(root: any): Laya.Camera | null {
        if (!root) {
            return null;
        }
        if (root instanceof Laya.Camera) {
            return root as Laya.Camera;
        }
        const count = typeof root.numChildren === "number" ? root.numChildren : 0;
        for (let i = 0; i < count; i++) {
            const found = this.findFirstCamera(root.getChildAt(i));
            if (found) {
                return found;
            }
        }
        return null;
    }

    private resolveCaptureMode(command: CaptureCommand, camera: Laya.Camera, target: Laya.Sprite3D | null): "orbit_camera" | "rotate_target" {
        if (command.capture_mode === "orbit_camera" || command.capture_mode === "rotate_target") {
            return command.capture_mode;
        }
        if (target && this.isDescendantOf(target, camera)) {
            return "rotate_target";
        }
        return "orbit_camera";
    }

    private isDescendantOf(node: any, ancestor: any): boolean {
        let current = node ? node.parent : null;
        while (current) {
            if (current === ancestor) {
                return true;
            }
            current = current.parent;
        }
        return false;
    }

    private resolveCenter(command: CaptureCommand, target: Laya.Sprite3D | null): Laya.Vector3 {
        if (command.center && command.center.length >= 3) {
            return new Laya.Vector3(command.center[0], command.center[1], command.center[2]);
        }
        if (target) {
            const p = target.transform.position;
            return new Laya.Vector3(p.x, p.y, p.z);
        }
        return new Laya.Vector3(0, 0, 0);
    }

    private resolveRadius(command: CaptureCommand, target: Laya.Sprite3D | null): number {
        if (command.target_size && command.target_size.length >= 3) {
            const sx = command.target_size[0];
            const sy = command.target_size[1];
            const sz = command.target_size[2];
            return Math.max(0.1, Math.sqrt(sx * sx + sy * sy + sz * sz) * 0.5);
        }
        const bounds = target ? this.tryGetBounds(target) : null;
        if (bounds) {
            const ext = bounds.getExtent();
            return Math.max(0.1, Math.sqrt(ext.x * ext.x + ext.y * ext.y + ext.z * ext.z));
        }
        return 1.0;
    }

    private tryGetBounds(target: Laya.Sprite3D): Laya.Bounds | null {
        let result: Laya.Bounds | null = null;
        this.walk(target, (node: any) => {
            const renderer = node.meshRenderer || node.skinnedMeshRenderer || node.renderer;
            const bounds = renderer && renderer.bounds ? renderer.bounds as Laya.Bounds : null;
            if (!bounds) {
                return;
            }
            if (!result) {
                result = bounds.clone();
            } else {
                Laya.Bounds.merge(result, bounds, result);
            }
        });
        return result;
    }

    private walk(root: any, visit: (node: any) => void): void {
        if (!root) {
            return;
        }
        visit(root);
        const count = typeof root.numChildren === "number" ? root.numChildren : 0;
        for (let i = 0; i < count; i++) {
            this.walk(root.getChildAt(i), visit);
        }
    }

    private placeCamera(camera: Laya.Camera, center: Laya.Vector3, radius: number, view: CaptureView, command: CaptureCommand): void {
        const yaw = ((view.yaw || 0) + (command.yaw_offset || 0)) * Math.PI / 180.0;
        const pitch = ((view.pitch || 0) + (command.pitch_offset || 0)) * Math.PI / 180.0;
        const distance = Math.max(command.min_distance || 1.0, radius * (command.distance_scale || 2.2));
        const cosPitch = Math.cos(pitch);
        const offset = new Laya.Vector3(
            Math.sin(yaw) * cosPitch * distance,
            Math.sin(pitch) * distance,
            Math.cos(yaw) * cosPitch * distance,
        );
        camera.transform.position = new Laya.Vector3(
            center.x - offset.x,
            center.y - offset.y,
            center.z - offset.z,
        );
        camera.transform.lookAt(center, Laya.Vector3.Up, false, true);
    }

    private rotateTargetForView(target: Laya.Sprite3D, baseEuler: Laya.Vector3, view: CaptureView, command: CaptureCommand): void {
        const yawSign = typeof command.target_yaw_sign === "number" ? command.target_yaw_sign : -1;
        const pitchSign = typeof command.target_pitch_sign === "number" ? command.target_pitch_sign : -1;
        const baseYaw = typeof command.target_base_yaw === "number" ? command.target_base_yaw : 0;
        const basePitch = typeof command.target_base_pitch === "number" ? command.target_base_pitch : 0;
        const yaw = ((view.yaw || 0) + (command.yaw_offset || 0)) * yawSign;
        const pitch = ((view.pitch || 0) + (command.pitch_offset || 0)) * pitchSign;
        target.transform.localRotationEuler = new Laya.Vector3(
            basePitch + pitch,
            baseYaw + yaw,
            baseEuler.z,
        );
    }

    private async waitFrames(count: number): Promise<void> {
        for (let i = 0; i < count; i++) {
            await new Promise<void>((resolve) => Laya.timer.frameOnce(1, this, resolve));
        }
    }

    private async readPixels(renderTexture: Laya.RenderTexture, width: number, height: number): Promise<Uint8Array> {
        const pixels = new Uint8Array(width * height * 4);
        const maybePromise = renderTexture.getDataAsync(0, 0, width, height, pixels) as any;
        if (maybePromise && typeof maybePromise.then === "function") {
            await maybePromise;
            return pixels;
        }
        return renderTexture.getData(0, 0, width, height, pixels) as Uint8Array;
    }

    private resolveAlphaSource(command: CaptureCommand): "silhouette_mask" | "alpha_from_rgb" | "render_alpha" {
        if (command.alpha_source === "silhouette_mask" || command.alpha_source === "alpha_from_rgb" || command.alpha_source === "render_alpha") {
            return command.alpha_source;
        }
        if (command.transparent_background === false) {
            return "render_alpha";
        }
        return "silhouette_mask";
    }

    private applyMaterialPatch(command: CaptureCommand, fallbackTarget: Laya.Sprite3D | null): MaterialPatchResult {
        const patch = command.material_patch;
        if (!patch || !patch.values) {
            return { applied: false, materialCount: 0, valueCount: 0 };
        }
        try {
            const target = patch.target_name ? this.resolveTarget(patch.target_name) : fallbackTarget;
            if (!target) {
                return {
                    applied: false,
                    materialCount: 0,
                    valueCount: 0,
                    error: `material_patch target not found: ${patch.target_name || command.target_name || "(empty)"}`,
                };
            }
            const materials: Laya.Material[] = [];
            this.walk(target, (node: any) => {
                const renderer = node.meshRenderer || node.skinnedMeshRenderer || node.renderer;
                this.collectMaterials(renderer, materials);
                this.collectMaterials(node._renderNode, materials);
            });
            let valueCount = 0;
            for (const material of materials) {
                for (const key of Object.keys(patch.values)) {
                    this.setMaterialValue(material, key, patch.values[key]);
                    valueCount++;
                }
            }
            return { applied: materials.length > 0, materialCount: materials.length, valueCount };
        } catch (error) {
            return { applied: false, materialCount: 0, valueCount: 0, error: String(error) };
        }
    }

    private setMaterialValue(material: Laya.Material, name: string, value: number | number[] | boolean): void {
        if (typeof value === "number") {
            material.setFloat(name, value);
            return;
        }
        if (typeof value === "boolean") {
            material.setBool(name, value);
            return;
        }
        if (!Array.isArray(value)) {
            return;
        }
        if (value.length === 4) {
            if (name.toLowerCase().indexOf("color") >= 0) {
                material.setColor(name, new Laya.Color(value[0], value[1], value[2], value[3]));
            } else {
                material.setVector4(name, new Laya.Vector4(value[0], value[1], value[2], value[3]));
            }
        } else if (value.length === 3) {
            material.setVector3(name, new Laya.Vector3(value[0], value[1], value[2]));
        } else if (value.length === 2) {
            material.setVector2(name, new Laya.Vector2(value[0], value[1]));
        }
    }

    private collectMaterials(source: any, materials: Laya.Material[]): void {
        if (!source) {
            return;
        }
        const sharedMaterials = source.sharedMaterials || source._materials || null;
        if (sharedMaterials) {
            for (const material of sharedMaterials as Laya.Material[]) {
                if (material && materials.indexOf(material) < 0) {
                    materials.push(material);
                }
            }
            return;
        }
        if (source.sharedMaterial && materials.indexOf(source.sharedMaterial) < 0) {
            materials.push(source.sharedMaterial);
        }
    }

    private async renderSilhouetteMask(camera: Laya.Camera, renderTexture: Laya.RenderTexture, target: Laya.Sprite3D, width: number, height: number): Promise<Uint8Array> {
        const previousClearColor = camera.clearColor ? camera.clearColor.clone() : null;
        const maskMaterial = new Laya.UnlitMaterial();
        maskMaterial.albedoColor = new Laya.Color(1, 1, 1, 1);
        maskMaterial.albedoIntensity = 1;
        const states = this.applyMaskRenderState(target, maskMaterial);
        try {
            camera.clearColor = new Laya.Color(0, 0, 0, 1);
            await this.waitFrames(2);
            return await this.readPixels(renderTexture, width, height);
        } finally {
            this.restoreRenderState(states);
            if (previousClearColor) {
                camera.clearColor = previousClearColor;
            }
            maskMaterial.destroy();
        }
    }

    private applyMaskRenderState(target: Laya.Sprite3D, maskMaterial: Laya.Material): RendererState[] {
        const targetSources = this.collectRenderSources(target);
        const targetSet = new Set<any>(targetSources);
        const allSources = this.collectRenderSources(this.sceneRoot());
        const states: RendererState[] = [];
        for (const source of allSources) {
            const materials = this.getSourceMaterials(source);
            states.push({
                source,
                enabled: typeof source.enabled === "boolean" ? source.enabled : null,
                materials: materials ? materials.slice() : null,
            });
            if (targetSet.has(source)) {
                const count = Math.max(1, materials ? materials.length : 1);
                const maskMaterials: Laya.Material[] = [];
                for (let i = 0; i < count; i++) {
                    maskMaterials.push(maskMaterial);
                }
                this.setSourceMaterials(source, maskMaterials);
                if (typeof source.enabled === "boolean") {
                    source.enabled = true;
                }
            } else if (typeof source.enabled === "boolean") {
                source.enabled = false;
            }
        }
        return states;
    }

    private restoreRenderState(states: RendererState[]): void {
        for (const state of states) {
            if (state.materials) {
                this.setSourceMaterials(state.source, state.materials);
            }
            if (state.enabled !== null) {
                state.source.enabled = state.enabled;
            }
        }
    }

    private collectRenderSources(root: any): any[] {
        const sources: any[] = [];
        this.walk(root, (node: any) => {
            const renderer = node.meshRenderer || node.skinnedMeshRenderer || node.renderer;
            this.addRenderSource(renderer, sources);
            this.addRenderSource(node._renderNode, sources);
        });
        return sources;
    }

    private addRenderSource(source: any, sources: any[]): void {
        if (!source || sources.indexOf(source) >= 0) {
            return;
        }
        if (this.getSourceMaterials(source) || typeof source.enabled === "boolean") {
            sources.push(source);
        }
    }

    private getSourceMaterials(source: any): Laya.Material[] | null {
        if (!source) {
            return null;
        }
        return (source.sharedMaterials || source._materials || (source.sharedMaterial ? [source.sharedMaterial] : null)) as Laya.Material[] | null;
    }

    private setSourceMaterials(source: any, materials: Laya.Material[]): void {
        if (!source) {
            return;
        }
        if (source.sharedMaterials !== undefined) {
            source.sharedMaterials = materials;
        } else if (source._materials !== undefined) {
            source._materials = materials;
        } else if (source.sharedMaterial !== undefined) {
            source.sharedMaterial = materials[0] || null;
        }
    }

    private applyMaskAlpha(pixels: Uint8Array, maskPixels: Uint8Array, mode?: "binary" | "soft", threshold?: number): void {
        const binary = mode !== "soft";
        const minValue = typeof threshold === "number" ? Math.max(0, Math.min(255, threshold)) : 1;
        const count = Math.min(pixels.length, maskPixels.length);
        for (let i = 0; i < count; i += 4) {
            const maskValue = Math.max(maskPixels[i], maskPixels[i + 1], maskPixels[i + 2]);
            pixels[i + 3] = binary ? (maskValue >= minValue ? 255 : 0) : maskValue;
        }
    }

    private zeroTransparentRgb(pixels: Uint8Array): void {
        for (let i = 0; i < pixels.length; i += 4) {
            if (pixels[i + 3] === 0) {
                pixels[i] = 0;
                pixels[i + 1] = 0;
                pixels[i + 2] = 0;
            }
        }
    }

    private liftRgbIntoAlpha(pixels: Uint8Array, threshold?: number): void {
        const minValue = typeof threshold === "number" ? Math.max(0, Math.min(255, threshold)) : 1;
        for (let i = 0; i < pixels.length; i += 4) {
            if (pixels[i + 3] !== 0) {
                continue;
            }
            const maxRgb = Math.max(pixels[i], pixels[i + 1], pixels[i + 2]);
            if (maxRgb < minValue) {
                continue;
            }
            pixels[i + 3] = maxRgb;
            const scale = 255 / maxRgb;
            pixels[i] = Math.min(255, Math.round(pixels[i] * scale));
            pixels[i + 1] = Math.min(255, Math.round(pixels[i + 1] * scale));
            pixels[i + 2] = Math.min(255, Math.round(pixels[i + 2] * scale));
        }
    }

    private pixelsToPngDataUrl(pixels: Uint8Array, width: number, height: number, flipY: boolean): string {
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext("2d");
        if (!context) {
            throw new Error("2D canvas context is unavailable");
        }
        const imageData = context.createImageData(width, height);
        const target = imageData.data;
        for (let y = 0; y < height; y++) {
            const sourceY = flipY ? height - 1 - y : y;
            const sourceOffset = sourceY * width * 4;
            const targetOffset = y * width * 4;
            target.set(pixels.subarray(sourceOffset, sourceOffset + width * 4), targetOffset);
        }
        context.putImageData(imageData, 0, 0);
        return canvas.toDataURL("image/png");
    }

    private async postImage(command: CaptureCommand, view: CaptureView, index: number, dataUrl: string, width: number, height: number, patchResult: MaterialPatchResult): Promise<void> {
        const url = command.post_url || `${command.server_base_url || this.serverBaseUrl}/material-fit/capture-result`;
        const viewId = view.view_id || view.id || `view_${this.pad(index, 3)}`;
        await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                nonce: command.nonce,
                view_id: viewId,
                file_name: view.file_name || `${viewId}.png`,
                width,
                height,
                yaw: view.yaw,
                pitch: view.pitch || 0,
                transparent_background: command.transparent_background !== false,
                alpha_source: this.resolveAlphaSource(command),
                material_patch: patchResult,
                png_base64: dataUrl.replace(/^data:image\/png;base64,/, ""),
            }),
        });
    }

    private async postLog(kind: string, message: string): Promise<void> {
        try {
            await fetch(`${this.serverBaseUrl}/material-fit/capture-log`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ kind, message, nonce: this._lastNonce, at: Date.now() }),
            });
        } catch {
            // Logging must never break capture.
        }
    }

    private pad(value: number, width: number): string {
        let text = String(value);
        while (text.length < width) {
            text = "0" + text;
        }
        return text;
    }
}
