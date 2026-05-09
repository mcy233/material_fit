import type {
  CaseOverviewPayload,
  CaseSummary,
  FileInfo,
  FilePickResult,
  IterationDetail,
  IterationSummary,
  JobState,
  LayaControlSchemaPresetList,
  PreanalysisPayload,
  PreflightResult,
  ProjectDetail,
  ProjectSummary,
} from './types';
import { getJson, sendJson } from './api/client';

export function fetchCases(): Promise<CaseSummary[]> {
  return getJson<CaseSummary[]>('/api/cases');
}

export function fetchCaseOverview(caseId: string): Promise<CaseOverviewPayload> {
  return getJson<CaseOverviewPayload>(`/api/cases/${encodeURIComponent(caseId)}/overview`);
}

export function fetchIterations(caseId: string): Promise<IterationSummary[]> {
  return getJson<IterationSummary[]>(`/api/cases/${encodeURIComponent(caseId)}/iterations`);
}

export function fetchIterationDetail(caseId: string, iterId: string): Promise<IterationDetail> {
  return getJson<IterationDetail>(
    `/api/cases/${encodeURIComponent(caseId)}/iterations/${encodeURIComponent(iterId)}`,
  );
}

export interface CaseReportPayload {
  case_id: string;
  report_path: string;
  case_dir: string;
  image_base: string;
  text: string;
}

export function fetchCaseReport(caseId: string): Promise<CaseReportPayload> {
  return getJson<CaseReportPayload>(`/api/cases/${encodeURIComponent(caseId)}/report`);
}

// ============================================================================
// Project lifecycle
// ============================================================================

export function fetchProjects(): Promise<ProjectSummary[]> {
  return getJson<ProjectSummary[]>('/api/projects');
}

export function createProject(payload: {
  id: string;
  name: string;
  description?: string;
}): Promise<ProjectDetail> {
  return sendJson<ProjectDetail>('/api/projects', 'POST', payload);
}

export function fetchProject(projectId: string): Promise<ProjectDetail> {
  return getJson<ProjectDetail>(`/api/projects/${encodeURIComponent(projectId)}`);
}

export function patchProject(
  projectId: string,
  patch: Record<string, unknown>,
): Promise<ProjectDetail> {
  return sendJson<ProjectDetail>(
    `/api/projects/${encodeURIComponent(projectId)}`,
    'PATCH',
    patch,
  );
}

export function deleteProject(projectId: string): Promise<{ id: string; trash_path: string }> {
  return sendJson<{ id: string; trash_path: string }>(
    `/api/projects/${encodeURIComponent(projectId)}`,
    'DELETE',
  );
}

// ============================================================================
// File picker (native dialog) + filesystem peek
// ============================================================================

export function pickFile(payload: {
  mode?: 'open' | 'save' | 'directory';
  title?: string;
  initial_dir?: string;
  initial_file?: string;
  filetypes?: [string, string][];
}): Promise<FilePickResult> {
  return sendJson<FilePickResult>('/api/files/pick', 'POST', payload);
}

export interface RegionPickResult {
  region: { x: number; y: number; width: number; height: number } | null;
  error?: string;
  // E-008 follow-up: when pickRegion is called with a laya_window
  // descriptor, the backend also returns the Laya window's current
  // rect (so the frontend can save it for diagnostic purposes) and
  // a precomputed anchor (region position relative to the Laya
  // window's top-left). The frontend is expected to write the anchor
  // back into project.inputs.laya_capture_anchor so future captures
  // survive Laya window drags/resizes.
  laya_window_rect?: { left: number; top: number; right: number; bottom: number } | null;
  anchor?: { offset_x: number; offset_y: number; width: number; height: number } | null;
  anchor_error?: string;
}

export function pickRegion(payload?: {
  laya_window?: { process_pattern?: string; title_pattern?: string };
}): Promise<RegionPickResult> {
  return sendJson<RegionPickResult>('/api/files/pick_region', 'POST', payload ?? {});
}

export function fileInfo(path: string): Promise<FileInfo> {
  return getJson<FileInfo>(`/api/files/info?path=${encodeURIComponent(path)}`);
}

export function externalPreviewUrl(path: string): string {
  return `/api/files/preview?path=${encodeURIComponent(path)}`;
}

// ============================================================================
// Preanalysis
// ============================================================================

export function runPreanalysis(
  projectId: string,
  options: { use_llm?: boolean } = {},
): Promise<PreanalysisPayload> {
  return sendJson<PreanalysisPayload>(
    `/api/projects/${encodeURIComponent(projectId)}/preanalyze`,
    'POST',
    options,
  );
}

export function fetchPreanalysis(projectId: string): Promise<PreanalysisPayload> {
  return getJson<PreanalysisPayload>(
    `/api/projects/${encodeURIComponent(projectId)}/preanalysis`,
  );
}

// ============================================================================
// Preflight: Laya refresh probe (E-007)
// ============================================================================

export function runLayaRefreshPreflight(
  projectId: string,
  options: {
    probe_param?: string;
    mean_diff_change_threshold?: number;
    mean_diff_restore_threshold?: number;
  } = {},
): Promise<PreflightResult> {
  return sendJson<PreflightResult>(
    `/api/projects/${encodeURIComponent(projectId)}/preflight/laya_refresh`,
    'POST',
    options,
  );
}

export function fetchLastLayaRefreshPreflight(
  projectId: string,
): Promise<PreflightResult | null> {
  return fetch(
    `/api/projects/${encodeURIComponent(projectId)}/preflight/laya_refresh`,
    { headers: { Accept: 'application/json' } },
  ).then(async (r) => {
    if (r.status === 404) return null;
    if (!r.ok) {
      const text = await r.text().catch(() => '');
      throw new Error(`fetchLastLayaRefreshPreflight failed: ${r.status} ${text}`);
    }
    return (await r.json()) as PreflightResult;
  });
}

export function setManualMapping(
  projectId: string,
  mapping: Record<string, string>,
): Promise<PreanalysisPayload> {
  return sendJson<PreanalysisPayload>(
    `/api/projects/${encodeURIComponent(projectId)}/manual_mapping`,
    'PUT',
    { manual_param_mapping: mapping },
  );
}

export function saveLayaControlSchema(
  projectId: string,
  manualSchema: Record<string, unknown>,
): Promise<PreanalysisPayload> {
  return sendJson<PreanalysisPayload>(
    `/api/projects/${encodeURIComponent(projectId)}/laya_control_schema`,
    'PUT',
    { manual_laya_control_schema: manualSchema },
  );
}

export function fetchLayaControlSchemaPresets(projectId: string): Promise<LayaControlSchemaPresetList> {
  return getJson<LayaControlSchemaPresetList>(
    `/api/projects/${encodeURIComponent(projectId)}/laya_control_schema_presets`,
  );
}

export function applyLayaControlSchemaPreset(
  projectId: string,
  presetId: string,
): Promise<PreanalysisPayload> {
  return sendJson<PreanalysisPayload>(
    `/api/projects/${encodeURIComponent(projectId)}/laya_control_schema_presets/apply`,
    'POST',
    { preset_id: presetId },
  );
}

export function saveLayaControlSchemaPreset(
  projectId: string,
  payload: { name: string; description?: string },
): Promise<LayaControlSchemaPresetList> {
  return sendJson<LayaControlSchemaPresetList>(
    `/api/projects/${encodeURIComponent(projectId)}/laya_control_schema_presets`,
    'POST',
    payload,
  );
}

export function renameLayaControlSchemaPreset(
  projectId: string,
  presetId: string,
  payload: { name: string; description?: string },
): Promise<LayaControlSchemaPresetList> {
  return sendJson<LayaControlSchemaPresetList>(
    `/api/projects/${encodeURIComponent(projectId)}/laya_control_schema_presets/${encodeURIComponent(presetId)}`,
    'PUT',
    payload,
  );
}

export function deleteLayaControlSchemaPreset(
  projectId: string,
  presetId: string,
): Promise<LayaControlSchemaPresetList> {
  return sendJson<LayaControlSchemaPresetList>(
    `/api/projects/${encodeURIComponent(projectId)}/laya_control_schema_presets/${encodeURIComponent(presetId)}`,
    'DELETE',
  );
}

// ============================================================================
// Jobs
// ============================================================================

export function startJob(
  projectId: string,
  overrides?: Record<string, unknown>,
): Promise<JobState> {
  return sendJson<JobState>(
    `/api/projects/${encodeURIComponent(projectId)}/jobs`,
    'POST',
    overrides ?? {},
  );
}

export function listJobs(projectId: string): Promise<JobState[]> {
  return getJson<JobState[]>(`/api/projects/${encodeURIComponent(projectId)}/jobs`);
}

export function fetchJob(jobId: string): Promise<JobState> {
  return getJson<JobState>(`/api/jobs/${encodeURIComponent(jobId)}`);
}

export function cancelJob(jobId: string): Promise<JobState> {
  return sendJson<JobState>(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, 'POST');
}

export function fetchJobLog(jobId: string, tailKb = 64): Promise<{ text: string; job_id: string }> {
  return getJson(`/api/jobs/${encodeURIComponent(jobId)}/log?tail_kb=${tailKb}`);
}
