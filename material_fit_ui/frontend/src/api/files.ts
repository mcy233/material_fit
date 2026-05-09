import type { FileInfo, FilePickResult } from '../types';
import { getJson, sendJson } from './client';

export interface RegionPickResult {
  region: { x: number; y: number; width: number; height: number } | null;
  laya_window_rect?: { x: number; y: number; width: number; height: number } | null;
  anchor?: { enabled: boolean; offset_x: number; offset_y: number; width: number; height: number } | null;
}

export const pickFile = (payload: {
  mode?: 'open' | 'save' | 'directory';
  title?: string;
  initial_dir?: string;
  initial_file?: string;
  filetypes?: [string, string][];
}): Promise<FilePickResult> => sendJson<FilePickResult>('/api/files/pick', 'POST', payload);

export const pickRegion = (payload?: { laya_window?: Record<string, unknown> }): Promise<RegionPickResult> =>
  sendJson<RegionPickResult>('/api/files/pick_region', 'POST', payload ?? {});

export const fileInfo = (path: string): Promise<FileInfo> =>
  getJson<FileInfo>(`/api/files/info?path=${encodeURIComponent(path)}`);

export const externalPreviewUrl = (path: string): string =>
  `/api/files/preview?path=${encodeURIComponent(path)}`;
