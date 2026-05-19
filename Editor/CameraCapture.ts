console.log("[CameraCapture] CameraCapture.ts loaded");

type MaterialFitCaptureCommand = {
    auto_capture?: boolean;
    nonce?: string;
    capture_kind?: "selected_camera" | "multiview";
    refresh_assets?: string[];
    reload_scene_after_reimport?: boolean;
    refresh_after_reimport_delay_ms?: number;
};

@IEditor.regClass()
export class CameraCapture {
    private static readonly COMMAND_FILE = "material_fit_capture_command.json";
    private static _autoTimer: any = null;
    private static _autoBusy: boolean = false;
    private static _lastAutoNonce: string = "";

    @IEditor.onLoad
    static onLoad() {
        console.log("[CameraCapture] onLoad start");

        try {
            Editor.extensionManager.addMenuItem(
                "App/tools/screenshotViewport",
                () => {
                    console.log("[CameraCapture] >>> VIEWPORT menu clicked");
                    CameraCapture.runViewport();
                },
                { label: "截图当前场景视口" }
            );
            console.log("[CameraCapture] addMenuItem ok: screenshotViewport");
        } catch (e) {
            console.error("[CameraCapture] addMenuItem screenshotViewport failed:", e);
        }

        try {
            Editor.extensionManager.addMenuItem(
                "App/tools/screenshotSelectedCamera",
                () => {
                    console.log("[CameraCapture] >>> SELECTED CAMERA menu clicked");
                    CameraCapture.runSelectedCamera();
                },
                { label: "按选中相机截图" }
            );
            console.log("[CameraCapture] addMenuItem ok: screenshotSelectedCamera");
        } catch (e) {
            console.error("[CameraCapture] addMenuItem screenshotSelectedCamera failed:", e);
        }

        try {
            Editor.extensionManager.addMenuItem(
                "App/tools/screenshotMaterialFitMultiview",
                () => {
                    console.log("[CameraCapture] >>> MATERIAL FIT MULTIVIEW menu clicked");
                    CameraCapture.runMaterialFitMultiview();
                },
                { label: "按命令多视角截图" }
            );
            console.log("[CameraCapture] addMenuItem ok: screenshotMaterialFitMultiview");
        } catch (e) {
            console.error("[CameraCapture] addMenuItem screenshotMaterialFitMultiview failed:", e);
        }

        CameraCapture._startAutoCapturePolling();
    }

    static async runViewport() {
        await CameraCapture._invoke("CameraCaptureEnv.captureToFile", "视口截图");
    }

    static async runSelectedCamera() {
        await CameraCapture._invoke("CameraCaptureEnv.captureFromSelectedCamera", "选中相机截图");
    }

    static async runMaterialFitMultiview() {
        await CameraCapture._invoke("CameraCaptureEnv.captureMultiviewFromCommand", "多视角截图");
    }

    private static _startAutoCapturePolling(): void {
        if (CameraCapture._autoTimer) {
            return;
        }
        CameraCapture._autoTimer = setInterval(() => {
            CameraCapture._pollAutoCapture();
        }, 1000);
        console.log("[CameraCapture] auto capture polling started");
    }

    private static async _pollAutoCapture(): Promise<void> {
        if (CameraCapture._autoBusy) {
            return;
        }
        try {
            const fs = IEditor.require("fs");
            const commandPath = CameraCapture._resolveCommandPath();
            if (!commandPath || !fs.existsSync(commandPath)) {
                return;
            }

            const command = JSON.parse(fs.readFileSync(commandPath, "utf8")) as MaterialFitCaptureCommand;
            if (!command.auto_capture || !command.nonce || command.nonce === CameraCapture._lastAutoNonce) {
                return;
            }

            CameraCapture._autoBusy = true;
            CameraCapture._lastAutoNonce = command.nonce;
            console.log(`[CameraCapture] auto capture triggered: ${command.nonce}`);
            await CameraCapture._runMaterialFitCommand(command);
        } catch (e) {
            console.error("[CameraCapture] auto capture error:", e);
        } finally {
            CameraCapture._autoBusy = false;
        }
    }

    private static async _runMaterialFitCommand(command: MaterialFitCaptureCommand): Promise<void> {
        await CameraCapture._reimportAssets(command.refresh_assets || []);
        if (command.reload_scene_after_reimport) {
            await CameraCapture._reloadActiveScene();
        }

        const delayMs = Math.max(0, command.refresh_after_reimport_delay_ms || 500);
        if (delayMs > 0) {
            await new Promise(resolve => setTimeout(resolve, delayMs));
        }

        const script = command.capture_kind === "selected_camera"
            ? "CameraCaptureEnv.captureSelectedCameraFromCommand"
            : "CameraCaptureEnv.captureMultiviewFromCommand";
        const label = command.capture_kind === "selected_camera" ? "自动相机截图" : "自动多视角截图";
        await CameraCapture._invoke(script, label);
    }

    private static async _reimportAssets(assetPaths: string[]): Promise<void> {
        if (!assetPaths.length) {
            return;
        }

        const assets: IEditor.IAssetInfo[] = [];
        for (const assetPath of assetPaths) {
            const asset = await Editor.assetDb.getAsset(assetPath, true);
            if (!asset) {
                throw new Error(`刷新资源失败，未找到资源: ${assetPath}`);
            }
            assets.push(asset);
        }

        console.log(`[CameraCapture] reimport assets: ${assetPaths.join(", ")}`);
        Editor.assetDb.reimport(assets);
        await Editor.assetDb.flushChanges();
    }

    private static async _reloadActiveScene(): Promise<void> {
        const scene = Editor.sceneManager.activeScene;
        if (!scene) {
            return;
        }
        console.log(`[CameraCapture] reload active scene: ${scene.sceneId}`);
        await Editor.sceneManager.reloadScene(scene.sceneId);
    }

    private static _resolveCommandPath(): string | null {
        const fs = IEditor.require("fs");
        for (const candidate of CameraCapture._commandPathCandidates()) {
            if (fs.existsSync(candidate)) {
                return candidate;
            }
        }
        return null;
    }

    private static _commandPathCandidates(): string[] {
        const pathMod = IEditor.require("path");
        const projectPath = Editor.projectPath;
        return [
            pathMod.join(projectPath, CameraCapture.COMMAND_FILE),
            pathMod.join(projectPath, "assets", CameraCapture.COMMAND_FILE),
            pathMod.join(pathMod.dirname(projectPath), CameraCapture.COMMAND_FILE),
            pathMod.join(pathMod.dirname(projectPath), "assets", CameraCapture.COMMAND_FILE),
        ];
    }

    private static async _invoke(script: string, label: string) {
        console.log(`[CameraCapture] invoke ${script}`);
        Editor.showToast(`${label}开始...`, "info", undefined, 1500);

        let result: { ok: boolean; path?: string; error?: string };
        try {
            result = await Editor.scene.runScript(script);
            console.log(`[CameraCapture] ${script} result:`, result);
        } catch (e) {
            console.error(`[CameraCapture] ${script} threw:`, e);
            Editor.showToast(`${label}失败: ${(e as Error).message}`, "error", undefined, 5000);
            return;
        }

        if (result && result.ok) {
            Editor.showToast(`${label}已保存: ${result.path}`, "info", undefined, 5000);
        } else {
            Editor.showToast(`${label}失败: ${(result && result.error) || "未知错误"}`, "error", undefined, 5000);
        }
    }
}
