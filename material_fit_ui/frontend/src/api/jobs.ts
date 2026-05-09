import type { JobState } from '../types';
import { getJson, sendJson } from './client';

export const startJob = (
  projectId: string,
  overrides: Record<string, unknown> = {},
): Promise<JobState> =>
  sendJson<JobState>(`/api/projects/${encodeURIComponent(projectId)}/jobs`, 'POST', overrides);

export const listJobs = (projectId: string): Promise<JobState[]> =>
  getJson<JobState[]>(`/api/projects/${encodeURIComponent(projectId)}/jobs`);

export const fetchJob = (jobId: string): Promise<JobState> =>
  getJson<JobState>(`/api/jobs/${encodeURIComponent(jobId)}`);

export const cancelJob = (jobId: string): Promise<JobState> =>
  sendJson<JobState>(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, 'POST');

export const fetchJobLog = (
  jobId: string,
  tailKb = 64,
): Promise<{ text: string; job_id: string }> =>
  getJson<{ text: string; job_id: string }>(
    `/api/jobs/${encodeURIComponent(jobId)}/log?tail_kb=${tailKb}`,
  );
