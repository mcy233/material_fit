console.log("[CameraCaptureEnv] CameraCaptureEnv.ts loaded");

type CaptureView = {
    id?: string;
    view_id?: string;
    yaw: number;
    pitch?: number;
    file_name?: string;
};

type CaptureCommand = {
    output_dir?: string;
    output_file_name?: string;
    camera_name?: string;
    target_name?: string;
    width?: number;
    height?: number;
    center?: number[];
    target_size?: number[];
    distance_scale?: number;
    min_distance?: number;
    fov?: number;
    use_orthographic?: boolean;
    orthographic_vertical_size?: number;
    capture_mode?: "auto" | "orbit_camera" | "rotate_target";
    yaw_offset?: number;
    pitch_offset?: number;
    target_yaw_sign?: number;
    target_pitch_sign?: number;
    target_base_yaw?: number;
    target_base_pitch?: number;
    refresh_delay_ms?: number;
    align_target_bounds?: boolean;
    target_local_z?: number;
    zero_transparent_rgb?: boolean;
    render_backend?: "draw_scene" | "camera_render_target";
    render_texture_srgb?: boolean;
    capture_debug_mode?: "normal" | "color_only";
    alpha_source?: "silhouette_mask" | "alpha_from_rgb" | "render_alpha";
    alpha_from_rgb_threshold?: number;
    mask_alpha_mode?: "binary" | "soft";
    mask_alpha_threshold?: number;
    auto_capture?: boolean;
    capture_kind?: "selected_camera" | "multiview";
    material_patch?: MaterialPatch;
    views?: CaptureView[];
};

type MaterialPatch = {
    target_name?: string;
    values?: { [name: string]: number | number[] | boolean };
};

@IEditorEnv.regClass()
export class CameraCaptureEnv {
    private static readonly COMMAND_FILE = "material_fit_capture_command.json";
    private static _autoTimer: any = null;
    private static _autoBusy: boolean = false;
    private static _lastAutoNonce: string = "";

    @IEditorEnv.onLoad
    static onLoad() {
        console.log("[CameraCaptureEnv] onLoad: ready");
        // Auto scheduling runs in CameraCapture.ts so asset reimport can happen before rendering.
    }

    static async captureToFile(): Promise<{ ok: boolean; path?: string; error?: string }> {
        console.log("[CameraCaptureEnv] captureToFile called");
        try {
            const d3 = EditorEnv.d3Manager;
            if (!d3 || !d3.sceneRT) {
                return { ok: false, error: "场景渲染目标不可用（请在场景编辑器中打开一个场景）" };
            }

            d3.refresh();
            await new Promise(resolve => setTimeout(resolve, 50));

            const rt = d3.sceneRT;
            const width = rt.width | 0;
            const height = rt.height | 0;
            console.log(`[CameraCaptureEnv] sceneRT size = ${width}x${height}`);
            if (width <= 0 || height <= 0) {
                return { ok: false, error: `视口尺寸无效 ${width}x${height}` };
            }

            const pixels = new Uint8Array(width * height * 4);
            await rt.getDataAsync(0, 0, width, height, pixels);

            const filePath = await CameraCaptureEnv._savePng(pixels, width, height, "scene");
            console.log(`[CameraCaptureEnv] saved -> ${filePath}`);
            return { ok: true, path: filePath };
        } catch (e) {
            console.error("[CameraCaptureEnv] error:", e);
            return { ok: false, error: (e as Error).message || String(e) };
        }
    }

    static async captureFromSelectedCamera(): Promise<{ ok: boolean; path?: string; error?: string }> {
        console.log("[CameraCaptureEnv] captureFromSelectedCamera called");
        return CameraCaptureEnv._captureCameraToFile({ requireSelection: true, prefix: "pose" });
    }

    static async captureSelectedCameraFromCommand(): Promise<{ ok: boolean; path?: string; error?: string }> {
        console.log("[CameraCaptureEnv] captureSelectedCameraFromCommand called");
        try {
            const fs = IEditorEnv.require("fs");
            const pathMod = IEditorEnv.require("path");
            const commandPath = CameraCaptureEnv._resolveCommandPath();
            if (!commandPath) {
                return { ok: false, error: `命令文件不存在: ${CameraCaptureEnv._commandPathCandidates().join(" | ")}` };
            }
            const command = JSON.parse(fs.readFileSync(commandPath, "utf8")) as CaptureCommand;
            const outputDir = CameraCaptureEnv._resolveOutputDir(command.output_dir);
            if (!fs.existsSync(outputDir)) {
                fs.mkdirSync(outputDir, { recursive: true });
            }
            const fileName = CameraCaptureEnv._safeFileName(command.output_file_name || "selected_camera.png");
            const result = await CameraCaptureEnv._captureCameraToFile({
                requireSelection: false,
                cameraName: command.camera_name || "Capture Camera",
                outputDir,
                fileName,
                zeroTransparentRgb: command.zero_transparent_rgb !== false,
                targetName: command.target_name || "model",
                renderBackend: command.render_backend || "draw_scene",
                renderTextureSrgb: command.render_texture_srgb !== false,
                captureDebugMode: command.capture_debug_mode || "normal",
                alphaSource: command.alpha_source || "silhouette_mask",
                alphaFromRgbThreshold: command.alpha_from_rgb_threshold,
                maskAlphaMode: command.mask_alpha_mode,
                maskAlphaThreshold: command.mask_alpha_threshold,
            });
            if (!result.ok || !result.path) {
                return result;
            }
            const reportPath = pathMod.join(outputDir, "laya_editor_selected_camera_report.json");
            fs.writeFileSync(reportPath, JSON.stringify({
                ok: true,
                command_path: commandPath,
                output_dir: outputDir,
                camera_name: command.camera_name || "Capture Camera",
                file: result.path,
                files: [result.path],
                render_diagnostics: result.diagnostics,
            }, null, 2), "utf8");
            return result;
        } catch (e) {
            console.error("[CameraCaptureEnv] captureSelectedCameraFromCommand error:", e);
            return { ok: false, error: (e as Error).message || String(e) };
        }
    }

    private static async _captureCameraToFile(options: {
        requireSelection: boolean;
        cameraName?: string;
        outputDir?: string;
        fileName?: string;
        prefix?: string;
        zeroTransparentRgb?: boolean;
        targetName?: string;
        renderBackend?: "draw_scene" | "camera_render_target";
        renderTextureSrgb?: boolean;
        captureDebugMode?: "normal" | "color_only";
        alphaSource?: "silhouette_mask" | "alpha_from_rgb" | "render_alpha";
        alphaFromRgbThreshold?: number;
        maskAlphaMode?: "binary" | "soft";
        maskAlphaThreshold?: number;
    }): Promise<{ ok: boolean; path?: string; error?: string; diagnostics?: any }> {
        let tempRT: Laya.RenderTexture | null = null;
        try {
            let camera: Laya.Camera | null = null;
            if (options.requireSelection) {
                const selection = EditorEnv.scene.selection;
                for (const node of selection) {
                    if (node instanceof Laya.Camera) {
                        camera = node;
                        break;
                    }
                }
            } else {
                camera = CameraCaptureEnv._resolveCamera(options.cameraName || "Capture Camera");
            }
            if (!camera) {
                return { ok: false, error: options.requireSelection ? "请先在场景中选中一个 Camera 节点" : `未找到截图相机: ${options.cameraName || "Capture Camera"}` };
            }

            const scene3D = EditorEnv.scene.scene3D;
            if (!scene3D) {
                return { ok: false, error: "当前没有可用的 3D 场景" };
            }

            const sceneRT = EditorEnv.d3Manager?.sceneRT;
            const width = (sceneRT?.width | 0) || 1280;
            const height = (sceneRT?.height | 0) || 720;
            console.log(`[CameraCaptureEnv] capture size = ${width}x${height}, camera = ${camera.name}`);
            const diagnostics = CameraCaptureEnv._collectRenderDiagnostics(scene3D, camera);
            const target = options.targetName ? CameraCaptureEnv._findNodeByName(scene3D, options.targetName) as Laya.Sprite3D : null;

            tempRT = Laya.RenderTexture.createFromPool(
                width, height,
                Laya.RenderTargetFormat.R8G8B8A8,
                Laya.RenderTargetFormat.DEPTH_16,
                false, 1, false, options.renderTextureSrgb !== false
            );

            const previousClearColor = camera.clearColor ? camera.clearColor.clone() : null;
            try {
                if (options.alphaSource !== "render_alpha") {
                    camera.clearColor = new Laya.Color(0, 0, 0, 1);
                }
                await CameraCaptureEnv._renderCameraToTexture(camera, scene3D, tempRT, options.renderBackend || "draw_scene");
            } finally {
                if (previousClearColor) {
                    camera.clearColor = previousClearColor;
                }
            }

            const pixels = new Uint8Array(width * height * 4);
            await tempRT.getDataAsync(0, 0, width, height, pixels);
            if (options.captureDebugMode === "color_only") {
                // Diagnostic mode: save the camera RenderTexture bytes exactly as read.
            } else if (options.alphaSource === "alpha_from_rgb") {
                CameraCaptureEnv._liftRgbIntoAlpha(pixels, options.alphaFromRgbThreshold);
            } else if (options.alphaSource !== "render_alpha" && target) {
                const maskPixels = await CameraCaptureEnv._renderSilhouetteMask(camera, scene3D, tempRT, target, width, height, target.transform.localRotationEuler.clone());
                CameraCaptureEnv._applyMaskAlpha(pixels, maskPixels, options.maskAlphaMode, options.maskAlphaThreshold);
            }

            const filePath = options.outputDir && options.fileName
                ? await CameraCaptureEnv._savePngToDir(pixels, width, height, options.outputDir, options.fileName, options.captureDebugMode !== "color_only" && options.zeroTransparentRgb !== false)
                : await CameraCaptureEnv._savePng(pixels, width, height, options.prefix || "pose");
            console.log(`[CameraCaptureEnv] saved -> ${filePath}`);
            return { ok: true, path: filePath, diagnostics };
        } catch (e) {
            console.error("[CameraCaptureEnv] error:", e);
            return { ok: false, error: (e as Error).message || String(e) };
        } finally {
            if (tempRT) {
                Laya.RenderTexture.recoverToPool(tempRT);
            }
        }
    }

    static async captureMultiviewFromCommand(): Promise<{ ok: boolean; path?: string; error?: string; count?: number }> {
        console.log("[CameraCaptureEnv] captureMultiviewFromCommand called");
        let tempRT: Laya.RenderTexture | null = null;
        try {
            const fs = IEditorEnv.require("fs");
            const pathMod = IEditorEnv.require("path");
            const commandPath = CameraCaptureEnv._resolveCommandPath();
            if (!commandPath) {
                return { ok: false, error: `命令文件不存在: ${CameraCaptureEnv._commandPathCandidates().join(" | ")}` };
            }

            const command = JSON.parse(fs.readFileSync(commandPath, "utf8")) as CaptureCommand;
            const scene3D = EditorEnv.scene.scene3D;
            if (!scene3D) {
                return { ok: false, error: "当前没有可用的 3D 场景" };
            }

            const camera = CameraCaptureEnv._resolveCamera(command.camera_name);
            if (!camera) {
                return { ok: false, error: `未找到截图相机: ${command.camera_name || "当前选中 Camera"}` };
            }

            const target = command.target_name ? CameraCaptureEnv._findNodeByName(scene3D, command.target_name) as Laya.Sprite3D : null;
            if (command.target_name && !target) {
                return { ok: false, error: `未找到目标模型: ${command.target_name}` };
            }
            if (!target && !command.center) {
                return { ok: false, error: "需要 target_name 或 center 才能执行多视角截图" };
            }

            const sceneRT = EditorEnv.d3Manager?.sceneRT;
            const width = Math.max(1, (command.width | 0) || (sceneRT?.width | 0) || 1280);
            const height = Math.max(1, (command.height | 0) || (sceneRT?.height | 0) || 720);
            const views = command.views && command.views.length > 0
                ? command.views
                : [{ view_id: "v000_yaw0_pitch0", yaw: 0, pitch: 0, file_name: "laya_v000_yaw0_pitch0.png" }];
            const outputDir = CameraCaptureEnv._resolveOutputDir(command.output_dir);
            if (!fs.existsSync(outputDir)) {
                fs.mkdirSync(outputDir, { recursive: true });
            }

            const center = CameraCaptureEnv._resolveCenter(command, target);
            const radius = CameraCaptureEnv._resolveRadius(command, target);
            const captureMode = CameraCaptureEnv._resolveCaptureMode(command, camera, target);
            const refreshDelayMs = Math.max(0, typeof command.refresh_delay_ms === "number" ? command.refresh_delay_ms : 80);
            const diagnostics = CameraCaptureEnv._collectRenderDiagnostics(scene3D, camera);
            const previousFov = camera.fieldOfView;
            const previousOrthographic = camera.orthographic;
            const previousOrthographicVerticalSize = camera.orthographicVerticalSize;
            const previousClearColor = camera.clearColor ? camera.clearColor.clone() : null;
            const previousCameraPosition = camera.transform.position.clone();
            const previousCameraEuler = camera.transform.rotationEuler.clone();
            const previousTargetEuler = target ? target.transform.localRotationEuler.clone() : null;
            const previousTargetLocalPosition = target ? target.transform.localPosition.clone() : null;

            tempRT = Laya.RenderTexture.createFromPool(
                width, height,
                Laya.RenderTargetFormat.R8G8B8A8,
                Laya.RenderTargetFormat.DEPTH_16,
                false, 1, false, command.render_texture_srgb !== false
            );

            if (typeof command.fov === "number" && command.fov > 0) {
                camera.fieldOfView = command.fov;
            }
            if (typeof command.use_orthographic === "boolean") {
                camera.orthographic = command.use_orthographic;
            }
            if (typeof command.orthographic_vertical_size === "number" && command.orthographic_vertical_size > 0) {
                camera.orthographicVerticalSize = command.orthographic_vertical_size;
            }
            if (captureMode === "rotate_target" && target && command.align_target_bounds !== false) {
                CameraCaptureEnv._alignChildTargetToCamera(target, command);
            }
            if (command.alpha_source !== "render_alpha") {
                camera.clearColor = new Laya.Color(0, 0, 0, 1);
            }
            const materialPatchResult = CameraCaptureEnv._applyMaterialPatch(command, target);

            const saved: string[] = [];
            const viewReports: any[] = [];
            try {
                for (let index = 0; index < views.length; index++) {
                    const view = views[index];
                    let rotationDiagnostics: any = null;
                    if (captureMode === "rotate_target") {
                        if (!target || !previousTargetEuler) {
                            throw new Error("rotate_target 模式需要 target_name");
                        }
                        rotationDiagnostics = CameraCaptureEnv._rotateTargetForView(target, previousTargetEuler, view, command);
                    } else {
                        CameraCaptureEnv._placeCamera(camera, center, radius, view, command);
                    }

                    await CameraCaptureEnv._settleEditorRenderState(refreshDelayMs);
                    if (rotationDiagnostics && target) {
                        rotationDiagnostics.after_settle_local_euler = CameraCaptureEnv._vec3ToArray(target.transform.localRotationEuler);
                        rotationDiagnostics.after_settle_world_euler = CameraCaptureEnv._vec3ToArray(target.transform.rotationEuler);
                    }
                    if (captureMode === "rotate_target" && target && previousTargetEuler) {
                        CameraCaptureEnv._rotateTargetForView(target, previousTargetEuler, view, command);
                        if (rotationDiagnostics) {
                            rotationDiagnostics.after_reassert_local_euler = CameraCaptureEnv._vec3ToArray(target.transform.localRotationEuler);
                            rotationDiagnostics.after_reassert_world_euler = CameraCaptureEnv._vec3ToArray(target.transform.rotationEuler);
                        }
                    }
                    const renderLockedLocalEuler = target ? target.transform.localRotationEuler.clone() : null;
                    if (captureMode === "rotate_target" && target && previousTargetEuler) {
                        CameraCaptureEnv._rotateTargetForView(target, previousTargetEuler, view, command);
                        if (rotationDiagnostics) {
                            rotationDiagnostics.before_render_local_euler = CameraCaptureEnv._vec3ToArray(target.transform.localRotationEuler);
                            rotationDiagnostics.before_render_world_euler = CameraCaptureEnv._vec3ToArray(target.transform.rotationEuler);
                        }
                    }
                    const renderBackendUsed = await CameraCaptureEnv._renderCameraToTexture(camera, scene3D, tempRT, command.render_backend || "draw_scene");

                    const pixels = new Uint8Array(width * height * 4);
                    await tempRT.getDataAsync(0, 0, width, height, pixels);
                    if (target && renderLockedLocalEuler) {
                        target.transform.localRotationEuler = renderLockedLocalEuler;
                    }
                    const debugColorOnly = command.capture_debug_mode === "color_only";
                    if (debugColorOnly) {
                        if (rotationDiagnostics) {
                            rotationDiagnostics.after_color_only_local_euler = target ? CameraCaptureEnv._vec3ToArray(target.transform.localRotationEuler) : null;
                            rotationDiagnostics.after_color_only_world_euler = target ? CameraCaptureEnv._vec3ToArray(target.transform.rotationEuler) : null;
                        }
                    } else if (command.alpha_source === "alpha_from_rgb") {
                        CameraCaptureEnv._liftRgbIntoAlpha(pixels, command.alpha_from_rgb_threshold);
                    } else if (command.alpha_source !== "render_alpha" && target) {
                        const lockedLocalEuler = renderLockedLocalEuler || target.transform.localRotationEuler.clone();
                        const maskPixels = await CameraCaptureEnv._renderSilhouetteMask(camera, scene3D, tempRT, target, width, height, lockedLocalEuler);
                        CameraCaptureEnv._applyMaskAlpha(pixels, maskPixels, command.mask_alpha_mode, command.mask_alpha_threshold);
                        if (rotationDiagnostics) {
                            rotationDiagnostics.after_mask_local_euler = CameraCaptureEnv._vec3ToArray(target.transform.localRotationEuler);
                            rotationDiagnostics.after_mask_world_euler = CameraCaptureEnv._vec3ToArray(target.transform.rotationEuler);
                        }
                    }

                    const viewId = view.view_id || view.id || `view_${CameraCaptureEnv._pad(index, 3)}`;
                    const fileName = CameraCaptureEnv._safeFileName(view.file_name || `${viewId}.png`);
                    const filePath = await CameraCaptureEnv._savePngToDir(pixels, width, height, outputDir, fileName, !debugColorOnly && command.zero_transparent_rgb !== false);
                    saved.push(filePath);
                    viewReports.push({
                        index,
                        view_id: viewId,
                        requested_yaw: view.yaw || 0,
                        requested_pitch: view.pitch || 0,
                        file: filePath,
                        target_local_rotation_euler: target ? CameraCaptureEnv._vec3ToArray(target.transform.localRotationEuler) : null,
                        target_rotation_euler: target ? CameraCaptureEnv._vec3ToArray(target.transform.rotationEuler) : null,
                        rotation_diagnostics: rotationDiagnostics,
                        camera_position: CameraCaptureEnv._vec3ToArray(camera.transform.position),
                        camera_rotation_euler: CameraCaptureEnv._vec3ToArray(camera.transform.rotationEuler),
                        render_backend: renderBackendUsed,
                        capture_debug_mode: command.capture_debug_mode || "normal",
                        alpha_source: command.alpha_source || "silhouette_mask",
                    });
                    console.log(`[CameraCaptureEnv] multiview saved ${index + 1}/${views.length}: ${filePath}`);
                }
            } finally {
                camera.fieldOfView = previousFov;
                camera.orthographic = previousOrthographic;
                camera.orthographicVerticalSize = previousOrthographicVerticalSize;
                camera.transform.position = previousCameraPosition;
                camera.transform.rotationEuler = previousCameraEuler;
                if (previousClearColor) {
                    camera.clearColor = previousClearColor;
                }
                if (target && previousTargetEuler) {
                    target.transform.localRotationEuler = new Laya.Vector3(command.target_base_pitch || 0, command.target_base_yaw || 0, previousTargetEuler.z);
                }
                if (target && previousTargetLocalPosition) {
                    target.transform.localPosition = previousTargetLocalPosition;
                }
                await CameraCaptureEnv._waitFrames(1);
            }

            const reportPath = pathMod.join(outputDir, "laya_editor_multiview_report.json");
            fs.writeFileSync(reportPath, JSON.stringify({
                ok: true,
                command_path: commandPath,
                output_dir: outputDir,
                capture_mode: captureMode,
                render_backend: command.render_backend || "draw_scene",
                render_texture_srgb: command.render_texture_srgb !== false,
                capture_debug_mode: command.capture_debug_mode || "normal",
                material_patch: materialPatchResult,
                width,
                height,
                count: saved.length,
                files: saved,
                views: viewReports,
                render_diagnostics: diagnostics,
            }, null, 2), "utf8");

            return { ok: true, path: outputDir, count: saved.length };
        } catch (e) {
            console.error("[CameraCaptureEnv] captureMultiviewFromCommand error:", e);
            return { ok: false, error: (e as Error).message || String(e) };
        } finally {
            if (tempRT) {
                Laya.RenderTexture.recoverToPool(tempRT);
            }
        }
    }

    private static _startAutoCapturePolling(): void {
        if (CameraCaptureEnv._autoTimer) {
            return;
        }
        CameraCaptureEnv._autoTimer = setInterval(() => {
            CameraCaptureEnv._pollAutoCapture();
        }, 1000);
    }

    private static async _pollAutoCapture(): Promise<void> {
        if (CameraCaptureEnv._autoBusy) {
            return;
        }
        try {
            const fs = IEditorEnv.require("fs");
            const commandPath = CameraCaptureEnv._resolveCommandPath();
            if (!commandPath || !fs.existsSync(commandPath)) {
                return;
            }
            const command = JSON.parse(fs.readFileSync(commandPath, "utf8")) as CaptureCommand & { nonce?: string };
            if (!command.auto_capture || !command.nonce || command.nonce === CameraCaptureEnv._lastAutoNonce) {
                return;
            }
            CameraCaptureEnv._autoBusy = true;
            CameraCaptureEnv._lastAutoNonce = command.nonce;
            console.log(`[CameraCaptureEnv] auto capture triggered: ${command.nonce}`);
            const result = await CameraCaptureEnv.captureMultiviewFromCommand();
            console.log("[CameraCaptureEnv] auto capture result:", result);
        } catch (e) {
            console.error("[CameraCaptureEnv] auto capture error:", e);
        } finally {
            CameraCaptureEnv._autoBusy = false;
        }
    }

    private static async _savePng(pixels: Uint8Array, width: number, height: number, prefix: string): Promise<string> {
        const fs = IEditorEnv.require("fs");
        const pathMod = IEditorEnv.require("path");
        const sharp = IEditorEnv.require("sharp");

        const screenshotsDir = pathMod.join(EditorEnv.projectPath, "Screenshots");
        if (!fs.existsSync(screenshotsDir)) {
            fs.mkdirSync(screenshotsDir, { recursive: true });
        }

        const now = new Date();
        const pad = (n: number) => (n < 10 ? "0" + n : "" + n);
        const fileName = `${prefix}_${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
            + `_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}.png`;
        const filePath = pathMod.join(screenshotsDir, fileName);

        const pixelBuffer = Buffer.from(pixels.buffer, pixels.byteOffset, pixels.byteLength);
        await sharp(pixelBuffer, {
            raw: { width, height, channels: 4 }
        })
            .png()
            .toFile(filePath);
        return filePath;
    }

    private static async _savePngToDir(pixels: Uint8Array, width: number, height: number, outputDir: string, fileName: string, zeroTransparentRgb: boolean): Promise<string> {
        const pathMod = IEditorEnv.require("path");
        const sharp = IEditorEnv.require("sharp");
        const filePath = pathMod.join(outputDir, fileName);
        if (zeroTransparentRgb) {
            CameraCaptureEnv._zeroTransparentRgb(pixels);
        }
        const pixelBuffer = Buffer.from(pixels.buffer, pixels.byteOffset, pixels.byteLength);
        await sharp(pixelBuffer, {
            raw: { width, height, channels: 4 }
        })
            .png()
            .toFile(filePath);
        return filePath;
    }

    private static _zeroTransparentRgb(pixels: Uint8Array): void {
        for (let i = 0; i < pixels.length; i += 4) {
            if (pixels[i + 3] === 0) {
                pixels[i] = 0;
                pixels[i + 1] = 0;
                pixels[i + 2] = 0;
            }
        }
    }

    private static async _renderSilhouetteMask(
        camera: Laya.Camera,
        scene3D: Laya.Scene3D,
        renderTexture: Laya.RenderTexture,
        target: Laya.Sprite3D,
        width: number,
        height: number,
        lockedLocalEuler?: Laya.Vector3 | null,
    ): Promise<Uint8Array> {
        const previousClearColor = camera.clearColor ? camera.clearColor.clone() : null;
        const maskMaterial = new Laya.UnlitMaterial();
        maskMaterial.albedoColor = new Laya.Color(1, 1, 1, 1);
        const states = CameraCaptureEnv._applyMaskRenderState(target, maskMaterial);
        try {
            if (lockedLocalEuler) {
                target.transform.localRotationEuler = lockedLocalEuler;
            }
            camera.clearColor = new Laya.Color(0, 0, 0, 1);
            await CameraCaptureEnv._renderCameraToTexture(camera, scene3D, renderTexture, CameraCaptureEnv._currentRenderBackend);
            const maskPixels = new Uint8Array(width * height * 4);
            await renderTexture.getDataAsync(0, 0, width, height, maskPixels);
            return maskPixels;
        } finally {
            CameraCaptureEnv._restoreRenderState(states);
            if (previousClearColor) {
                camera.clearColor = previousClearColor;
            }
            if (lockedLocalEuler) {
                target.transform.localRotationEuler = lockedLocalEuler;
            }
            maskMaterial.destroy();
        }
    }

    private static _currentRenderBackend: "draw_scene" | "camera_render_target" = "draw_scene";

    private static async _renderCameraToTexture(
        camera: Laya.Camera,
        scene3D: Laya.Scene3D,
        renderTexture: Laya.RenderTexture,
        backend: "draw_scene" | "camera_render_target",
    ): Promise<"draw_scene" | "camera_render_target"> {
        CameraCaptureEnv._currentRenderBackend = backend;
        if (backend === "camera_render_target") {
            const previousTarget = camera.renderTarget;
            camera.renderTarget = renderTexture;
            try {
                const d3 = EditorEnv.d3Manager;
                if (d3 && typeof d3.refresh === "function") {
                    d3.refresh();
                }
                await CameraCaptureEnv._waitFrames(2);
                if (d3 && typeof d3.refresh === "function") {
                    d3.refresh();
                }
                await CameraCaptureEnv._waitFrames(1);
                return "camera_render_target";
            } finally {
                camera.renderTarget = previousTarget;
            }
        }
        Laya.Camera.drawRenderTextureByScene(camera, scene3D, renderTexture);
        Laya.Camera.drawRenderTextureByScene(camera, scene3D, renderTexture);
        return "draw_scene";
    }

    private static _applyMaskRenderState(target: Laya.Sprite3D, maskMaterial: Laya.Material): Array<{ source: any; enabled: boolean | null; materials: Laya.Material[] | null; sharedMaterial: Laya.Material | null }> {
        const targetSources = CameraCaptureEnv._collectRenderSources(target);
        const targetSet = new Set<any>(targetSources);
        const sceneRoot = EditorEnv.scene.scene3D as any;
        const allSources = CameraCaptureEnv._collectRenderSources(sceneRoot);
        const states: Array<{ source: any; enabled: boolean | null; materials: Laya.Material[] | null; sharedMaterial: Laya.Material | null }> = [];
        for (const source of allSources) {
            const materials = CameraCaptureEnv._getSourceMaterials(source);
            states.push({
                source,
                enabled: typeof source.enabled === "boolean" ? source.enabled : null,
                materials: materials ? materials.slice() : null,
                sharedMaterial: source.sharedMaterial || null,
            });
            if (targetSet.has(source)) {
                const count = Math.max(1, materials ? materials.length : 1);
                const maskMaterials: Laya.Material[] = [];
                for (let i = 0; i < count; i++) {
                    maskMaterials.push(maskMaterial);
                }
                CameraCaptureEnv._setSourceMaterials(source, maskMaterials);
                if (typeof source.enabled === "boolean") {
                    source.enabled = true;
                }
            } else if (typeof source.enabled === "boolean") {
                source.enabled = false;
            }
        }
        return states;
    }

    private static _restoreRenderState(states: Array<{ source: any; enabled: boolean | null; materials: Laya.Material[] | null; sharedMaterial: Laya.Material | null }>): void {
        for (const state of states) {
            if (state.enabled !== null) {
                state.source.enabled = state.enabled;
            }
            if (state.materials) {
                CameraCaptureEnv._setSourceMaterials(state.source, state.materials);
            } else if ("sharedMaterial" in state.source) {
                state.source.sharedMaterial = state.sharedMaterial;
            }
        }
    }

    private static _collectRenderSources(root: any): any[] {
        const sources: any[] = [];
        CameraCaptureEnv._walk(root, (node: any) => {
            for (const key of ["meshRenderer", "skinnedMeshRenderer", "renderer", "_renderNode"]) {
                const source = node ? node[key] : null;
                if (source && sources.indexOf(source) < 0 && (source.sharedMaterial || source.sharedMaterials || source._materials)) {
                    sources.push(source);
                }
            }
        });
        return sources;
    }

    private static _getSourceMaterials(source: any): Laya.Material[] | null {
        if (source.sharedMaterials && source.sharedMaterials.length !== undefined) {
            return Array.prototype.slice.call(source.sharedMaterials);
        }
        if (source._materials && source._materials.length !== undefined) {
            return Array.prototype.slice.call(source._materials);
        }
        if (source.sharedMaterial) {
            return [source.sharedMaterial];
        }
        return null;
    }

    private static _setSourceMaterials(source: any, materials: Laya.Material[]): void {
        if ("sharedMaterials" in source) {
            source.sharedMaterials = materials;
        } else if ("_materials" in source) {
            source._materials = materials;
        } else {
            source.sharedMaterial = materials[0] || null;
        }
    }

    private static _applyMaskAlpha(pixels: Uint8Array, maskPixels: Uint8Array, mode?: "binary" | "soft", threshold?: number): void {
        const binary = mode !== "soft";
        const minValue = typeof threshold === "number" ? Math.max(0, Math.min(255, threshold)) : 1;
        const count = Math.min(pixels.length, maskPixels.length);
        for (let i = 0; i < count; i += 4) {
            const maskValue = Math.max(maskPixels[i], maskPixels[i + 1], maskPixels[i + 2]);
            pixels[i + 3] = binary ? (maskValue >= minValue ? 255 : 0) : maskValue;
        }
    }

    private static _liftRgbIntoAlpha(pixels: Uint8Array, threshold?: number): void {
        const minValue = typeof threshold === "number" ? Math.max(0, Math.min(255, threshold)) : 1;
        for (let i = 0; i < pixels.length; i += 4) {
            if (pixels[i + 3] !== 0) {
                continue;
            }
            const rgbMax = Math.max(pixels[i], pixels[i + 1], pixels[i + 2]);
            if (rgbMax < minValue) {
                continue;
            }
            pixels[i + 3] = rgbMax;
            const scale = 255 / rgbMax;
            pixels[i] = Math.min(255, Math.round(pixels[i] * scale));
            pixels[i + 1] = Math.min(255, Math.round(pixels[i + 1] * scale));
            pixels[i + 2] = Math.min(255, Math.round(pixels[i + 2] * scale));
        }
    }

    private static _collectRenderDiagnostics(scene3D: any, camera: any): any {
        const directionQueue = scene3D ? scene3D._directionLights : null;
        const queueElements = directionQueue && directionQueue._elements ? directionQueue._elements : [];
        const queueLength = directionQueue && typeof directionQueue._length === "number"
            ? directionQueue._length
            : (Array.isArray(queueElements) ? queueElements.length : 0);
        const queueLights: any[] = [];
        for (let i = 0; i < queueLength; i++) {
            const light = queueElements[i];
            if (!light) {
                continue;
            }
            const owner = light.owner;
            queueLights.push({
                index: i,
                owner_name: owner ? owner.name : null,
                enabled: typeof light.enabled !== "undefined" ? light.enabled : null,
                strength: typeof light.strength !== "undefined" ? light.strength : null,
                intensity: typeof light.intensity !== "undefined" ? light.intensity : null,
                owner_scene_matches: owner ? owner.scene === scene3D : null,
            });
        }
        return {
            script_version: "material_fit_reassert_rotation_final_only_20260518",
            camera_name: camera ? camera.name : null,
            camera_scene_matches: camera ? camera.scene === scene3D : null,
            stat_enable_light: (Laya as any).Stat ? (Laya as any).Stat.enableLight : null,
            config3d_multi_lighting: (Laya as any).Config3D ? (Laya as any).Config3D._multiLighting : null,
            direction_light_queue_length: queueLength,
            direction_light_queue: queueLights,
            scene_direction_light_nodes: CameraCaptureEnv._collectSceneDirectionLightNodes(scene3D, scene3D),
        };
    }

    private static _collectSceneDirectionLightNodes(root: any, scene3D: any): any[] {
        const result: any[] = [];
        const walk = (node: any) => {
            if (!node) {
                return;
            }
            const componentType = (Laya as any).DirectionLightCom;
            const light = componentType && typeof node.getComponent === "function" ? node.getComponent(componentType) : null;
            if (light) {
                result.push({
                    node_name: node.name,
                    enabled: typeof light.enabled !== "undefined" ? light.enabled : null,
                    strength: typeof light.strength !== "undefined" ? light.strength : null,
                    intensity: typeof light.intensity !== "undefined" ? light.intensity : null,
                    owner_scene_matches: node.scene === scene3D,
                });
            }
            const count = typeof node.numChildren === "number" ? node.numChildren : 0;
            for (let i = 0; i < count; i++) {
                walk(node.getChildAt(i));
            }
        };
        walk(root);
        return result;
    }

    private static _resolveOutputDir(outputDir?: string): string {
        const pathMod = IEditorEnv.require("path");
        if (outputDir && pathMod.isAbsolute(outputDir)) {
            return outputDir;
        }
        if (outputDir) {
            return pathMod.resolve(EditorEnv.projectPath, outputDir);
        }
        return pathMod.join(EditorEnv.projectPath, "Screenshots", "material_fit_multiview");
    }

    private static _resolveCommandPath(): string | null {
        const fs = IEditorEnv.require("fs");
        for (const candidate of CameraCaptureEnv._commandPathCandidates()) {
            if (fs.existsSync(candidate)) {
                return candidate;
            }
        }
        return null;
    }

    private static _commandPathCandidates(): string[] {
        const pathMod = IEditorEnv.require("path");
        const projectPath = EditorEnv.projectPath;
        return [
            pathMod.join(projectPath, CameraCaptureEnv.COMMAND_FILE),
            pathMod.join(projectPath, "assets", CameraCaptureEnv.COMMAND_FILE),
            pathMod.join(pathMod.dirname(projectPath), CameraCaptureEnv.COMMAND_FILE),
            pathMod.join(pathMod.dirname(projectPath), "assets", CameraCaptureEnv.COMMAND_FILE),
        ];
    }

    private static _resolveCamera(name?: string): Laya.Camera | null {
        if (name) {
            const node = CameraCaptureEnv._findNodeByName(EditorEnv.scene.scene3D, name);
            return node instanceof Laya.Camera ? node as Laya.Camera : null;
        }
        const selection = EditorEnv.scene.selection || [];
        for (const node of selection) {
            if (node instanceof Laya.Camera) {
                return node;
            }
        }
        return CameraCaptureEnv._findFirstCamera(EditorEnv.scene.scene3D);
    }

    private static _findNodeByName(root: any, name: string): any {
        if (!root || !name) {
            return null;
        }
        if (root.name === name) {
            return root;
        }
        const count = typeof root.numChildren === "number" ? root.numChildren : 0;
        for (let i = 0; i < count; i++) {
            const found = CameraCaptureEnv._findNodeByName(root.getChildAt(i), name);
            if (found) {
                return found;
            }
        }
        return null;
    }

    private static _findFirstCamera(root: any): Laya.Camera | null {
        if (!root) {
            return null;
        }
        if (root instanceof Laya.Camera) {
            return root as Laya.Camera;
        }
        const count = typeof root.numChildren === "number" ? root.numChildren : 0;
        for (let i = 0; i < count; i++) {
            const found = CameraCaptureEnv._findFirstCamera(root.getChildAt(i));
            if (found) {
                return found;
            }
        }
        return null;
    }

    private static _resolveCaptureMode(command: CaptureCommand, camera: Laya.Camera, target: Laya.Sprite3D | null): "orbit_camera" | "rotate_target" {
        if (command.capture_mode === "orbit_camera" || command.capture_mode === "rotate_target") {
            return command.capture_mode;
        }
        if (target && CameraCaptureEnv._isDescendantOf(target, camera)) {
            return "rotate_target";
        }
        return "orbit_camera";
    }

    private static _isDescendantOf(node: any, ancestor: any): boolean {
        let current = node ? node.parent : null;
        while (current) {
            if (current === ancestor) {
                return true;
            }
            current = current.parent;
        }
        return false;
    }

    private static _resolveCenter(command: CaptureCommand, target: Laya.Sprite3D | null): Laya.Vector3 {
        if (command.center && command.center.length >= 3) {
            return new Laya.Vector3(command.center[0], command.center[1], command.center[2]);
        }
        if (target) {
            const p = target.transform.position;
            return new Laya.Vector3(p.x, p.y, p.z);
        }
        return new Laya.Vector3(0, 0, 0);
    }

    private static _resolveRadius(command: CaptureCommand, target: Laya.Sprite3D | null): number {
        if (command.target_size && command.target_size.length >= 3) {
            const sx = command.target_size[0];
            const sy = command.target_size[1];
            const sz = command.target_size[2];
            return Math.max(0.1, Math.sqrt(sx * sx + sy * sy + sz * sz) * 0.5);
        }
        const bounds = target ? CameraCaptureEnv._tryGetBounds(target) : null;
        if (bounds) {
            const ext = bounds.getExtent();
            return Math.max(0.1, Math.sqrt(ext.x * ext.x + ext.y * ext.y + ext.z * ext.z));
        }
        return 1.0;
    }

    private static _tryGetBounds(target: Laya.Sprite3D): Laya.Bounds | null {
        let result: Laya.Bounds | null = null;
        CameraCaptureEnv._walk(target, (node: any) => {
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

    private static _walk(root: any, visit: (node: any) => void): void {
        if (!root) {
            return;
        }
        visit(root);
        const count = typeof root.numChildren === "number" ? root.numChildren : 0;
        for (let i = 0; i < count; i++) {
            CameraCaptureEnv._walk(root.getChildAt(i), visit);
        }
    }

    private static _placeCamera(camera: Laya.Camera, center: Laya.Vector3, radius: number, view: CaptureView, command: CaptureCommand): void {
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

    private static _rotateTargetForView(target: Laya.Sprite3D, baseEuler: Laya.Vector3, view: CaptureView, command: CaptureCommand): any {
        const yawSign = typeof command.target_yaw_sign === "number" ? command.target_yaw_sign : -1;
        const pitchSign = typeof command.target_pitch_sign === "number" ? command.target_pitch_sign : -1;
        const baseYaw = typeof command.target_base_yaw === "number" ? command.target_base_yaw : 0;
        const basePitch = typeof command.target_base_pitch === "number" ? command.target_base_pitch : 0;
        const yaw = ((view.yaw || 0) + (command.yaw_offset || 0)) * yawSign;
        const pitch = ((view.pitch || 0) + (command.pitch_offset || 0)) * pitchSign;
        const assigned = new Laya.Vector3(
            basePitch + pitch,
            baseYaw + yaw,
            baseEuler.z,
        );
        const beforeLocal = target.transform.localRotationEuler.clone();
        const beforeWorld = target.transform.rotationEuler.clone();
        target.transform.localRotationEuler = assigned;
        return {
            mode: "absolute_local_rotation_euler",
            view_yaw: view.yaw || 0,
            view_pitch: view.pitch || 0,
            yaw_offset: command.yaw_offset || 0,
            pitch_offset: command.pitch_offset || 0,
            target_yaw_sign: yawSign,
            target_pitch_sign: pitchSign,
            target_base_yaw: baseYaw,
            target_base_pitch: basePitch,
            initial_local_euler_at_capture_start: CameraCaptureEnv._vec3ToArray(baseEuler),
            before_set_local_euler: CameraCaptureEnv._vec3ToArray(beforeLocal),
            before_set_world_euler: CameraCaptureEnv._vec3ToArray(beforeWorld),
            assigned_local_euler: CameraCaptureEnv._vec3ToArray(assigned),
            after_set_local_euler: CameraCaptureEnv._vec3ToArray(target.transform.localRotationEuler),
            after_set_world_euler: CameraCaptureEnv._vec3ToArray(target.transform.rotationEuler),
        };
    }

    private static _alignChildTargetToCamera(target: Laya.Sprite3D, command: CaptureCommand): void {
        const bounds = CameraCaptureEnv._tryGetBounds(target);
        if (!bounds) {
            return;
        }
        const boundsCenter = bounds.getCenter();
        const targetPosition = target.transform.position;
        const local = target.transform.localPosition.clone();
        local.x -= boundsCenter.x - targetPosition.x;
        local.y -= boundsCenter.y - targetPosition.y;
        if (typeof command.target_local_z === "number") {
            local.z = command.target_local_z;
        }
        target.transform.localPosition = local;
    }

    private static _applyMaterialPatch(command: CaptureCommand, fallbackTarget: Laya.Sprite3D | null): { applied: boolean; materialCount: number; valueCount: number; error?: string } {
        const patch = command.material_patch;
        if (!patch || !patch.values) {
            return { applied: false, materialCount: 0, valueCount: 0 };
        }
        try {
            const target = patch.target_name
                ? CameraCaptureEnv._findNodeByName(EditorEnv.scene.scene3D, patch.target_name) as Laya.Sprite3D
                : fallbackTarget;
            if (!target) {
                return { applied: false, materialCount: 0, valueCount: 0, error: `material_patch target not found: ${patch.target_name || command.target_name || "(empty)"}` };
            }

            const materials: Laya.Material[] = [];
            CameraCaptureEnv._walk(target, (node: any) => {
                const renderer = node.meshRenderer || node.skinnedMeshRenderer || node.renderer;
                const sharedMaterials = renderer && renderer.sharedMaterials ? renderer.sharedMaterials as Laya.Material[] : null;
                if (!sharedMaterials) {
                    if (renderer && renderer.sharedMaterial && materials.indexOf(renderer.sharedMaterial) < 0) {
                        materials.push(renderer.sharedMaterial);
                    }
                    return;
                }
                for (const material of sharedMaterials) {
                    if (material && materials.indexOf(material) < 0) {
                        materials.push(material);
                    }
                }
            });

            let valueCount = 0;
            for (const material of materials) {
                for (const key of Object.keys(patch.values)) {
                    CameraCaptureEnv._setMaterialValue(material, key, patch.values[key]);
                    valueCount++;
                }
            }
            console.log(`[CameraCaptureEnv] material_patch applied: materials=${materials.length}, values=${valueCount}`);
            return { applied: materials.length > 0, materialCount: materials.length, valueCount };
        } catch (e) {
            console.error("[CameraCaptureEnv] material_patch error:", e);
            return { applied: false, materialCount: 0, valueCount: 0, error: (e as Error).message || String(e) };
        }
    }

    private static _setMaterialValue(material: Laya.Material, name: string, value: number | number[] | boolean): void {
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

    private static async _refreshEditorScene(delayMs: number): Promise<void> {
        const d3 = EditorEnv.d3Manager;
        if (d3) {
            d3.refresh();
        }
        if (delayMs > 0) {
            await new Promise(resolve => setTimeout(resolve, delayMs));
        }
    }

    private static async _settleEditorRenderState(delayMs: number): Promise<void> {
        if (delayMs > 0) {
            await new Promise(resolve => setTimeout(resolve, delayMs));
        }
        await CameraCaptureEnv._waitFrames(1);
    }

    private static async _waitFrames(count: number): Promise<void> {
        for (let i = 0; i < count; i++) {
            await new Promise<void>((resolve) => Laya.timer.frameOnce(1, CameraCaptureEnv, resolve));
        }
    }

    private static _vec3ToArray(value: Laya.Vector3): number[] {
        return [value.x, value.y, value.z];
    }

    private static _safeFileName(fileName: string): string {
        return fileName.replace(/[\\/:*?"<>|]/g, "_").slice(0, 160) || "capture.png";
    }

    private static _pad(value: number, width: number): string {
        let text = String(value);
        while (text.length < width) {
            text = "0" + text;
        }
        return text;
    }
}
