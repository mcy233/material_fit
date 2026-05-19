import type { LayaControlSchemaPresetList, LayaProbeOptions, PreanalysisPayload, PreflightResult } from '../types';
import { getJson, sendJson } from './client';

export const runPreanalysis = (
  projectId: string,
  payload: { use_llm?: boolean } = {},
): Promise<PreanalysisPayload> =>
  sendJson<PreanalysisPayload>(
    `/api/projects/${encodeURIComponent(projectId)}/preanalyze`,
    'POST',
    payload,
  );

export const fetchPreanalysis = (projectId: string): Promise<PreanalysisPayload> =>
  getJson<PreanalysisPayload>(`/api/projects/${encodeURIComponent(projectId)}/preanalysis`);

export const setManualMapping = (
  projectId: string,
  mapping: Record<string, string>,
): Promise<PreanalysisPayload> =>
  sendJson<PreanalysisPayload>(
    `/api/projects/${encodeURIComponent(projectId)}/manual_mapping`,
    'PUT',
    { manual_param_mapping: mapping },
  );

export const saveLayaControlSchema = (
  projectId: string,
  manualSchema: Record<string, unknown>,
): Promise<PreanalysisPayload> =>
  sendJson<PreanalysisPayload>(
    `/api/projects/${encodeURIComponent(projectId)}/laya_control_schema`,
    'PUT',
    { manual_laya_control_schema: manualSchema },
  );

export const fetchLayaControlSchemaPresets = (projectId: string): Promise<LayaControlSchemaPresetList> =>
  getJson<LayaControlSchemaPresetList>(
    `/api/projects/${encodeURIComponent(projectId)}/laya_control_schema_presets`,
  );

export const runLayaRefreshPreflight = (
  projectId: string,
  payload: Record<string, unknown> = {},
): Promise<PreflightResult> =>
  sendJson<PreflightResult>(
    `/api/projects/${encodeURIComponent(projectId)}/preflight/laya_refresh`,
    'POST',
    payload,
  );

export const fetchLayaProbeOptions = (projectId: string): Promise<LayaProbeOptions> =>
  getJson<LayaProbeOptions>(`/api/projects/${encodeURIComponent(projectId)}/preflight/laya_probe_options`);
