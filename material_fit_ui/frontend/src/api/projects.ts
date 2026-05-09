import type { ProjectDetail, ProjectSummary } from '../types';
import { getJson, sendJson } from './client';

export const fetchProjects = (): Promise<ProjectSummary[]> => getJson<ProjectSummary[]>('/api/projects');

export const createProject = (payload: {
  id: string;
  name: string;
  description?: string;
}): Promise<ProjectDetail> => sendJson<ProjectDetail>('/api/projects', 'POST', payload);

export const fetchProject = (projectId: string): Promise<ProjectDetail> =>
  getJson<ProjectDetail>(`/api/projects/${encodeURIComponent(projectId)}`);

export const patchProject = (
  projectId: string,
  patch: Record<string, unknown>,
): Promise<ProjectDetail> =>
  sendJson<ProjectDetail>(`/api/projects/${encodeURIComponent(projectId)}`, 'PATCH', patch);

export const deleteProject = (projectId: string): Promise<{ id: string; trash_path: string }> =>
  sendJson<{ id: string; trash_path: string }>(
    `/api/projects/${encodeURIComponent(projectId)}`,
    'DELETE',
  );
