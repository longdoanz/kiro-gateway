// --- Auth ---

export interface LoginRequest {
  username: string;
  password: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

export interface RefreshRequest {
  refresh_token: string;
}

export interface GoogleLoginRequest {
  credential: string;
}

export interface JwtPayload {
  sub: string;
  role: "admin" | "user";
  username: string;
  can_create_gateway_key: boolean;
  exp: number;
  type: "access" | "refresh";
}

// --- User ---

export interface UserCreate {
  username: string;
  password: string;
  role?: "admin" | "user";
}

export interface UserUpdate {
  is_active?: boolean;
  password?: string;
  role?: "admin" | "user";
  can_create_gateway_key?: boolean;
}

export interface UserResponse {
  id: number;
  username: string;
  role: string;
  is_active: boolean;
  created_at: string;
  can_create_gateway_key: boolean;
}

export interface UserDetailResponse extends UserResponse {
  api_keys: ApiKeyResponse[];
}

// --- ApiKey ---

export interface ApiKeyCreate {
  raw_key: string;
  user_id?: number;
}

export interface ApiKeyResponse {
  id: number;
  user_id: number;
  kiro_user_id: string | null;
  kiro_email: string | null;
  key_prefix: string;
  key_suffix: string;
  is_active: boolean;
  is_system: boolean;
  use_proxy: boolean;
  created_at: string;
  current_usage: number;
  usage_limit: number;
  input_tokens: number;
  output_tokens: number;
  last_used_at: string | null;
}

export interface SystemKeyCreate {
  raw_key: string;
  use_proxy?: boolean;
  is_active?: boolean;
}

export interface SystemKeyUpdate {
  is_active?: boolean;
  use_proxy?: boolean;
}

export interface ApiKeyToggle {
  is_active: boolean;
}

// --- KeyUsage ---

export interface KeyUsageResponse {
  month: string;
  current_usage: number;
  usage_limit: number;
  last_synced_at: string | null;
  last_used_at: string | null;
}

// --- Overview ---

export interface DailyUsage {
  date: string;
  input_tokens: number;
  output_tokens: number;
}

export interface CreditTrendPoint {
  date: string;
  credits_used: number;
}

export interface OverviewResponse {
  total_input_tokens: number;
  total_output_tokens: number;
  total_credits_used: number;
  total_credits_limit: number;
  total_users: number;
  active_users: number;
  active_keys: number;
  daily_usage: DailyUsage[];
  credit_trend: CreditTrendPoint[];
  total_gateway_users: number;
  active_gateway_users: number;
  gateway_input_tokens: number;
  gateway_output_tokens: number;
}

// --- Config ---

export interface ModelOverrideRule {
  from: string;
  to: string | string[];
}

export interface SystemConfigResponse {
  enable_model_override: boolean;
  model_override_rules: ModelOverrideRule[];
  model_override_default: string;
  enable_usage_sharing: boolean;
  enable_nine_router_model_override: boolean;
  nine_router_model_override_rules: ModelOverrideRule[];
  nine_router_model_override_default: string;
  enable_nine_router_direct: boolean;
}

export interface SystemConfigUpdate {
  enable_model_override?: boolean;
  model_override_rules?: ModelOverrideRule[];
  model_override_default?: string;
  enable_usage_sharing?: boolean;
  enable_nine_router_model_override?: boolean;
  nine_router_model_override_rules?: ModelOverrideRule[];
  nine_router_model_override_default?: string;
  enable_nine_router_direct?: boolean;
}

// --- Models ---

export interface ModelInfo {
  id: string;
  source: "cache" | "fallback";
}

export interface ModelListResponse {
  models: ModelInfo[];
  total: number;
}

// --- Import ---

export interface ImportResult {
  imported: number;
  updated: number;
  errors: string[];
}

// --- Analytics ---

export interface DailySeries {
  date: string;
  input_tokens: number;
  output_tokens: number;
}

export interface UserTokenUsage {
  kiro_user_id: string;
  display_name: string;
  username: string | null;
  email: string | null;
  input_tokens: number;
  output_tokens: number;
}

export interface TopUser {
  rank: number;
  kiro_user_id: string;
  display_name: string;
  username: string | null;
  email: string | null;
  input_tokens: number;
  output_tokens: number;
  share_pct: number;
}

export interface TokenShare {
  kiro_user_id: string;
  display_name: string;
  username: string | null;
  email: string | null;
  input_tokens: number;
  output_tokens: number;
  pct: number;
}

export interface UserDailySeries {
  display_name: string;
  username: string | null;
  email: string | null;
  daily: DailySeries[];
}

export interface AnalyticsResponse {
  time_range: string;
  daily_series: DailySeries[];
  user_tokens: UserTokenUsage[];
  top_users: TopUser[];
  token_share: TokenShare[];
  user_daily_series: UserDailySeries[];
}

// --- Kiro User Credit Usage ---

export interface KiroUserCreditUsage {
  kiro_user_id: string;
  display_name: string;
  username: string | null;
  email: string | null;
  used_credit: number;
  quota: number;
  remaining: number;
  remaining_pct: number;
  shared_input_tokens: number;
  shared_output_tokens: number;
}

export interface KiroUserCreditUsageResponse {
  month: string;
  users: KiroUserCreditUsage[];
}

// --- GatewayKey ---

export interface GatewayKeyResponse {
  id: number;
  user_id: number;
  key_prefix: string;
  key_suffix: string;
  is_active: boolean;
  created_at: string;
}

export interface GatewayKeyCreated extends GatewayKeyResponse {
  raw_key: string;
}

// --- Gateway Key Analytics ---

export interface GatewayKeyDailySeries {
  date: string;
  input_tokens: number;
  output_tokens: number;
}

export interface GatewayKeyUserUsage {
  user_id: number;
  username: string;
  input_tokens: number;
  output_tokens: number;
  last_active_at: string | null;
}

export interface GatewayKeyAnalyticsResponse {
  time_range: string;
  total_input_tokens: number;
  total_output_tokens: number;
  total_gateway_users: number;
  active_gateway_users: number;
  daily_series: GatewayKeyDailySeries[];
  user_usages: GatewayKeyUserUsage[];
}
