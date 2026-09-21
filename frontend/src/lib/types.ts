/** Shapes returned by the management API. */

export interface User {
  authenticated: boolean;
  id?: number;
  username?: string;
  email?: string;
  full_name?: string;
  is_staff?: boolean;
  is_superuser?: boolean;
  /** True until an authenticator app is enrolled, when policy requires one. */
  totp_setup_required?: boolean;
}

export interface CertificateSummary {
  id: number;
  issuer: string;
  issuer_label: string;
  status: string;
  not_after: string | null;
  days_until_expiry: number | null;
  auto_renew: boolean;
  last_error: string;
}

export interface Domain {
  id: number;
  name: string;
  description: string;
  ssl_enabled: boolean;
  ssl_cert_path: string;
  ssl_key_path: string;
  force_ssl_redirect: boolean;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  rule_count?: number;
  active_rule_count?: number;
  certificate: CertificateSummary | null;
}

export interface BackendInstance {
  id: number;
  backend: number;
  address: string;
  port: number;
  weight: number;
  max_fails: number;
  fail_timeout: number;
  is_healthy: boolean;
  is_draining: boolean;
  state: "healthy" | "failing" | "draining" | "disabled";
  consecutive_failures: number;
  consecutive_successes: number;
  last_health_check: string | null;
  last_health_status_code: number | null;
  last_health_response_ms: number | null;
  is_active: boolean;
}

export interface Backend {
  id: number;
  name: string;
  description: string;
  lb_method: "round_robin" | "least_conn" | "ip_hash";
  lb_method_label: string;
  keepalive_connections: number;
  keepalive_requests: number;
  keepalive_timeout: number;
  drain_unhealthy: boolean;
  unhealthy_threshold: number;
  healthy_threshold: number;
  health_check_enabled: boolean;
  health_check_path: string;
  health_check_interval: number;
  health_check_timeout: number;
  is_active: boolean;
  instances: BackendInstance[];
  healthy_instance_count: number;
  total_instance_count: number;
  serving_instance_count: number;
}

export interface HeaderRoute {
  id: number;
  rule: number;
  backend: number;
  backend_name: string;
  header_name: string;
  header_value: string;
  description: string;
  is_active: boolean;
}

export interface RoutingRule {
  id: number;
  domain: number;
  domain_name: string;
  backend: number;
  backend_name: string;
  match_type: "path_prefix" | "exact_path" | "regex";
  match_type_label: string;
  match_value: string;
  nginx_location: string;
  priority: number;
  cache_enabled: boolean;
  cache_ttl: number;
  cache_bypass_auth: boolean;
  cache_key_headers: string[];
  cache_ignore_upstream_control: boolean;
  cache_allow_authenticated: boolean;
  cache_min_uses: number;
  strip_prefix: boolean;
  custom_headers: Record<string, string>;
  proxy_buffering: boolean;
  proxy_read_timeout: number;
  rate_limit_enabled: boolean;
  rate_limit_rps: number;
  rate_limit_burst: number;
  is_active: boolean;
  header_routes: HeaderRoute[];
}

export interface Certificate {
  id: number;
  domain: number;
  domain_name: string;
  issuer: "acme" | "self_signed" | "manual";
  issuer_label: string;
  status: string;
  status_label: string;
  acme_account: number | null;
  acme_account_email: string | null;
  subject_common_name: string;
  san_domains: string[];
  serial_number: string;
  fingerprint_sha256: string;
  not_before: string | null;
  not_after: string | null;
  days_until_expiry: number | null;
  is_due_for_renewal: boolean;
  auto_renew: boolean;
  renew_before_days: number;
  last_issued_at: string | null;
  last_attempt_at: string | null;
  last_error: string;
  attempt_count: number;
}

export interface AcmeAccount {
  id: number;
  email: string;
  directory_url: string;
  directory_label: string;
  is_staging: boolean;
  agreed_to_tos: boolean;
  is_default: boolean;
  registered_at: string | null;
  certificate_count: number;
}

export interface DeployLog {
  id: number;
  status: "pending" | "testing" | "deployed" | "failed" | "rolled_back";
  status_label: string;
  error_output: string;
  deployed_by_username: string | null;
  created_at: string;
  completed_at: string | null;
  duration_ms: number | null;
  config_snapshot?: string;
}

export interface CacheStats {
  available: boolean;
  reason?: string;
  hit_ratio: number | null;
  served_from_cache: number;
  cacheable_requests: number;
  bypassed: number;
  breakdown: Record<string, number>;
  disk_used_bytes: number;
  disk_max_bytes: number;
  disk_used_pct: number | null;
  bytes_from_backend: number;
  bytes_to_clients: number;
  bytes_saved: number;
}

export interface DomainTraffic {
  domain: string;
  requests: number;
  bytes_in: number;
  bytes_out: number;
  avg_response_ms: number;
  responses: Record<string, number>;
  error_rate: number | null;
  cache_hit_ratio: number | null;
}

export interface TrafficStats {
  available: boolean;
  reason?: string;
  uptime_seconds: number;
  nginx_version: string;
  connections: Record<string, number>;
  total_requests: number;
  domains: DomainTraffic[];
}

export interface UpstreamServer {
  server: string;
  requests: number;
  avg_response_ms: number;
  bytes_in: number;
  bytes_out: number;
  down: boolean;
  weight: number;
  error_rate: number | null;
  responses: Record<string, number>;
}

export interface UpstreamStats {
  available: boolean;
  reason?: string;
  upstreams: { upstream: string; requests: number; servers: UpstreamServer[] }[];
}

export interface HealthGridBackend {
  id: number;
  name: string;
  lb_method: string;
  drain_unhealthy: boolean;
  instances: {
    id: number;
    address: string;
    state: string;
    is_healthy: boolean;
    is_draining: boolean;
    weight: number;
    consecutive_failures: number;
    last_check: string | null;
    response_ms: number | null;
    status_code: number | null;
  }[];
  healthy_count: number;
  total_count: number;
  status: "healthy" | "degraded" | "down";
}

export interface DashboardSummary {
  domains: { total: number; active: number; ssl_enabled: number };
  backends: {
    total: number;
    instances_total: number;
    instances_healthy: number;
    instances_draining: number;
    avg_probe_ms: number | null;
  };
  routing: { total_rules: number; active_rules: number; cached_rules: number };
  certificates: {
    total: number;
    active: number;
    failed: number;
    pending: number;
    expiring_within_14d: number;
    expired: number;
    auto_renewing: number;
    next_expiry: { domain__name: string; not_after: string } | null;
  };
  deploys: {
    last_24h_total: number;
    last_24h_failed: number;
    last_deploy: { id: number; status: string; created_at: string } | null;
  };
}

export interface Overview {
  summary: DashboardSummary;
  traffic: TrafficStats;
  cache: CacheStats;
  upstreams: UpstreamStats;
  health: HealthGridBackend[];
}

export interface Series {
  labels: Record<string, string>;
  points: { t: number; v: number | null }[];
}

export interface TimeSeries {
  range_minutes: number;
  step: string;
  requests_per_second: Series[];
  gateway_added_ms: Series[];
  cache_hit_ratio: Series[];
  upstream_latency_ms: Series[];
  status_classes: Series[];
}

export interface GatewayStatus {
  config_valid: boolean;
  config_test_output: string;
  last_deploy: DeployLog | null;
  debounce_seconds: number;
}
