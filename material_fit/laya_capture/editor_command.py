from __future__ import annotations

import argparse
import json
from argparse import Namespace
from pathlib import Path

from material_fit.laya_capture.capture_server import build_command


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate material_fit_capture_command.json for the Laya editor extension.")
    parser.add_argument("--laya-project", required=True, help="Laya project root. The command file is written here by default.")
    parser.add_argument("--unity-metadata", help="Unity multi-view metadata JSON used to seed yaw/pitch views.")
    parser.add_argument("--command-json", help="Optional command JSON override.")
    parser.add_argument("--command-out", help="Output command JSON path. Defaults to <laya-project>/material_fit_capture_command.json.")
    parser.add_argument("--output-dir", required=True, help="Directory where the Laya editor extension saves PNG files.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--camera-name", default="")
    parser.add_argument("--target-name", default="model")
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    parser.add_argument("--center", default="")
    parser.add_argument("--target-size", default="")
    parser.add_argument("--distance-scale", type=float, default=2.2)
    parser.add_argument("--min-distance", type=float, default=1.0)
    parser.add_argument("--fov", type=float, default=None)
    parser.add_argument("--use-orthographic", choices=["auto", "true", "false"], default="auto")
    parser.add_argument("--orthographic-vertical-size", type=float, default=None)
    parser.add_argument("--capture-mode", choices=["auto", "orbit_camera", "rotate_target"], default="auto")
    parser.add_argument("--yaw-offset", type=float, default=0.0)
    parser.add_argument("--pitch-offset", type=float, default=0.0)
    parser.add_argument("--target-yaw-sign", type=float, default=-1.0)
    parser.add_argument("--target-pitch-sign", type=float, default=-1.0)
    parser.add_argument("--refresh-delay-ms", type=int, default=80)
    parser.add_argument("--align-target-bounds", action="store_true", help="Auto-center target bounds when target is under the capture camera.")
    parser.add_argument("--target-local-z", type=float, default=None, help="Optional target local Z used by rotate_target mode.")
    parser.add_argument("--auto-capture", action="store_true", help="Let the Laya editor extension poll and run this command automatically.")
    parser.add_argument("--refresh-asset", action="append", default=[], help="Asset path to reimport before capture, e.g. resources/play/fish/1580/mat/1580_body.lmat.")
    parser.add_argument("--reload-scene-after-reimport", action="store_true", help="Reload the active Laya scene after reimporting refresh assets.")
    parser.add_argument("--refresh-after-reimport-delay-ms", type=int, default=800)
    args = parser.parse_args()

    laya_project = Path(args.laya_project).resolve()
    command_out = Path(args.command_out).resolve() if args.command_out else laya_project / "material_fit_capture_command.json"
    command_out.parent.mkdir(parents=True, exist_ok=True)

    command_args = Namespace(
        unity_metadata=args.unity_metadata,
        command_json=args.command_json,
        output_dir=args.output_dir,
        host=args.host,
        port=args.port,
        camera_name=args.camera_name,
        target_name=args.target_name,
        width=args.width,
        height=args.height,
        center=args.center,
        target_size=args.target_size,
        distance_scale=args.distance_scale,
        min_distance=args.min_distance,
        fov=args.fov,
        use_orthographic=args.use_orthographic,
        orthographic_vertical_size=args.orthographic_vertical_size,
        capture_mode=args.capture_mode,
        yaw_offset=args.yaw_offset,
        pitch_offset=args.pitch_offset,
        target_yaw_sign=args.target_yaw_sign,
        target_pitch_sign=args.target_pitch_sign,
    )
    command = build_command(command_args, Path(args.output_dir).resolve())
    command["refresh_delay_ms"] = args.refresh_delay_ms
    command["align_target_bounds"] = bool(args.align_target_bounds)
    command["zero_transparent_rgb"] = True
    command["alpha_source"] = "silhouette_mask"
    command["alpha_from_rgb_threshold"] = 1.0
    command["mask_alpha_mode"] = "binary"
    command["mask_alpha_threshold"] = 1.0
    command["render_texture_srgb"] = True
    if args.target_local_z is not None:
        command["target_local_z"] = args.target_local_z
    if args.auto_capture:
        command["auto_capture"] = True
        command["nonce"] = f"manual-{int(__import__('time').time())}"
    if args.refresh_asset:
        command["refresh_assets"] = args.refresh_asset
        command["reload_scene_after_reimport"] = args.reload_scene_after_reimport
        command["refresh_after_reimport_delay_ms"] = args.refresh_after_reimport_delay_ms
    command.pop("server_base_url", None)
    command.pop("post_url", None)

    command_out.write_text(json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8")
    print(command_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
