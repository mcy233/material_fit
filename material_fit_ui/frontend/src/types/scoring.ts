export interface HumanAcceptSignals {
  score?: number | null;
  components?: Record<string, unknown> | null;
  weights?: Record<string, unknown> | null;
  inputs?: Record<string, unknown> | null;
}

export interface PerceptualSignals {
  weighted_mae?: number | null;
  ssim?: number | null;
  ssim_status?: string | null;
  fit_score?: number | null;
  fit_components?: Record<string, unknown> | null;
  branch_weights?: unknown;
  weights_used?: Record<string, unknown> | null;
  coverage?: number | null;
  diagnostics?: Record<string, unknown> | null;
  human_accept?: HumanAcceptSignals | null;
  auto_mask?: Record<string, unknown> | null;
}
