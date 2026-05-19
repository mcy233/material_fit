import type { FileInfo, FilePickResult } from '../types';
import { getJson, sendJson } from './client';

export const pickFile = (payload: {
  mode?: 'open' | 'open_many' | 'save' | 'directory';
  title?: string;
  initial_dir?: string;
  initial_file?: string;
  filetypes?: [string, string][];
}): Promise<FilePickResult> => sendJson<FilePickResult>('/api/files/pick', 'POST', payload);

export const fileInfo = (path: string): Promise<FileInfo> =>
  getJson<FileInfo>(`/api/files/info?path=${encodeURIComponent(path)}`);

export const externalPreviewUrl = (path: string): string =>
  `/api/files/preview?path=${encodeURIComponent(path)}`;
