import type { CaseOverviewPayload, CaseSummary, IterationDetail, IterationSummary } from '../types';
import { getJson } from './client';

export interface CaseReportPayload {
  case_id: string;
  report_path: string;
  case_dir: string;
  image_base: string;
  text: string;
}

export const fetchCases = (): Promise<CaseSummary[]> => getJson<CaseSummary[]>('/api/cases');

export const fetchCaseOverview = (caseId: string): Promise<CaseOverviewPayload> =>
  getJson<CaseOverviewPayload>(`/api/cases/${encodeURIComponent(caseId)}/overview`);

export const fetchIterations = (caseId: string): Promise<IterationSummary[]> =>
  getJson<IterationSummary[]>(`/api/cases/${encodeURIComponent(caseId)}/iterations`);

export const fetchIterationDetail = (caseId: string, iterId: string): Promise<IterationDetail> =>
  getJson<IterationDetail>(
    `/api/cases/${encodeURIComponent(caseId)}/iterations/${encodeURIComponent(iterId)}`,
  );

export const fetchCaseReport = (caseId: string): Promise<CaseReportPayload> =>
  getJson<CaseReportPayload>(`/api/cases/${encodeURIComponent(caseId)}/report`);
