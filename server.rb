#!/usr/bin/ruby
# Escalations Dashboard - Ruby edition of server.py, for Macs without Python.
# Uses only the Ruby that ships with macOS, plus /usr/bin/curl for HTTPS
# (the system Ruby's TLS stack can't handshake with sdpondemand.manageengine.com).
#
#     ruby server.rb        # then open http://localhost:8787
#
# Same .env, same /api/tickets payload and health rules as server.py (live mode only).

require "webrick"
require "json"
require "uri"
require "open3"

ROOT = File.expand_path(File.dirname(__FILE__))

env_file = File.join(ROOT, ".env")
if File.exist?(env_file)
  File.readlines(env_file).each do |line|
    line = line.strip
    next if line.empty? || line.start_with?("#") || !line.include?("=")
    k, v = line.split("=", 2)
    ENV[k.strip] ||= v.strip.delete_prefix('"').delete_suffix('"').delete_prefix("'").delete_suffix("'")
  end
end

def env(name, default = "") (ENV[name] || default).strip end
def env_list(name, default) env(name, default).split(",").map(&:strip).reject(&:empty?) end

DEFAULT_PRIORITIES = ["PSS-IN-P1 | CL 4 Hours", "SSD-IN-P1 | CL 8 Hours", "PSS-IN-P2 | CL 8 Hours",
                      "SSD-IN-P2 | CL 24 Hours", "PSS-SR-P1 | CL 2 days", "SSD-SR-P1 | CL 2 Days"].join(",")
FIELDS = %w[subject display_id priority status technician group site requester created_time due_by_time
            first_response_due_by_time is_overdue is_first_response_overdue last_updated_time
            responded_time request_type level]

CFG = {
  base_url: env("SDP_BASE_URL").chomp("/"),
  accounts_url: env("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com").chomp("/"),
  client_id: env("ZOHO_CLIENT_ID"),
  client_secret: env("ZOHO_CLIENT_SECRET"),
  refresh_token: env("ZOHO_REFRESH_TOKEN"),
  priorities: env_list("CRITICAL_PRIORITIES", DEFAULT_PRIORITIES),
  levels: env_list("LEVELS", ""),  # empty = all levels
  closed: env_list("CLOSED_STATUSES", "Closed,Resolved,Cancelled,Canceled,Unresolved").map(&:downcase),
  at_risk_hours: env("AT_RISK_HOURS", "4").to_f,
  stale_hours: env("STALE_HOURS", "24").to_f,
  refresh_seconds: env("REFRESH_SECONDS", "60").to_i,
  max_tickets: env("MAX_TICKETS", "1000").to_i,
  ticket_url: env("TICKET_URL_TEMPLATE"),
  port: env("PORT", "8787").to_i,
  host: env("HOST", "0.0.0.0"),
  # Optional HTTP basic auth for the whole dashboard (recommended on a shared server)
  auth_user: env("DASHBOARD_USER", "escalations"),
  auth_password: env("DASHBOARD_PASSWORD"),
}

if CFG[:base_url].empty? || CFG[:client_id].empty? || CFG[:client_secret].empty? || CFG[:refresh_token].empty?
  abort "Missing SDP_BASE_URL / ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_REFRESH_TOKEN in .env " \
        "(use `python3 server.py` for demo mode)."
end
if env("REQUIRE_PASSWORD").downcase == "true" && CFG[:auth_password].length < 8
  abort "REQUIRE_PASSWORD is on: set DASHBOARD_PASSWORD (8+ characters) before exposing ticket data."
end

# ------------------------------------------------------------------ HTTP via curl
# Secrets go to curl on stdin (-K -), never on the command line where `ps` could show them.
def curl(url, headers: {}, form: nil)
  cfg = +"silent\nshow-error\nmax-time = 45\nwrite-out = \"\\n%{http_code}\"\n"
  cfg << "url = #{url.to_json}\n"
  headers.each { |k, v| cfg << "header = #{"#{k}: #{v}".to_json}\n" }
  form&.each { |k, v| cfg << "data-urlencode = #{"#{k}=#{v}".to_json}\n" }
  out, err, st = Open3.capture3("/usr/bin/curl", "-K", "-", stdin_data: cfg)
  raise "curl failed: #{err.strip}" unless st.success?
  body, _, code = out.rpartition("\n")
  [code.to_i, body]
end

class SDPClient
  def initialize
    @token = nil
    @exp = 0
    @lock = Mutex.new
  end

  def access_token
    @lock.synchronize do
      return @token if @token && Time.now.to_f < @exp - 60
      code, body = curl("#{CFG[:accounts_url]}/oauth/v2/token", form: {
        "refresh_token" => CFG[:refresh_token], "client_id" => CFG[:client_id],
        "client_secret" => CFG[:client_secret], "grant_type" => "refresh_token" })
      j = JSON.parse(body) rescue {}
      raise "Zoho OAuth error (HTTP #{code}): #{j["error"] || body[0, 200]}" unless j["access_token"]
      @token = j["access_token"]
      @exp = Time.now.to_f + (j["expires_in"] || 3600).to_i
      @token
    end
  end

  def get(path, input)
    url = "#{CFG[:base_url]}/api/v3/#{path}?input_data=#{URI.encode_www_form_component(input.to_json)}"
    code, body = curl(url, headers: { "Accept" => "application/vnd.manageengine.sdp.v3+json",
                                      "Authorization" => "Zoho-oauthtoken #{access_token}" })
    if code != 200
      @token = nil if code == 401
      raise "SDP API HTTP #{code}: #{body[0, 500]}"
    end
    JSON.parse(body)
  end

  def open_critical_requests
    criteria = [
      { field: "priority.name", condition: "is", values: CFG[:priorities] },
      { field: "status.in_progress", condition: "is", values: [true], logical_operator: "AND" },
    ]
    unless CFG[:levels].empty?
      criteria << { field: "level.name", condition: "is", values: CFG[:levels], logical_operator: "AND" }
    end
    out = []
    start = 1
    while out.size < CFG[:max_tickets]
      body = get("requests", list_info: { row_count: 100, start_index: start, sort_field: "created_time",
                                          sort_order: "desc", get_total_count: true,
                                          fields_required: FIELDS, search_criteria: criteria })
      out.concat(body["requests"] || [])
      break unless body.dig("list_info", "has_more_rows")
      start += 100
    end
    out
  end
end

# ------------------------------------------------------------------ normalise (mirrors server.py)
def ms(f) f.is_a?(Hash) && f["value"] ? (Integer(f["value"]) rescue nil) : nil end
def nm(f) f.is_a?(Hash) ? f["name"] : nil end
def g(x) x == x.to_i ? x.to_i.to_s : x.to_s end

def ticket_link(id)
  return CFG[:ticket_url].gsub("{id}", id.to_s) unless CFG[:ticket_url].empty?
  base = CFG[:base_url].include?("/app/") ? CFG[:base_url] : "#{CFG[:base_url]}/app/itdesk"
  "#{base}/ui/requests/#{id}/details"
end

def classify(raw, now)
  st = raw["status"] || {}
  status = nm(st) || "Unknown"
  due = ms(raw["due_by_time"]); fr_due = ms(raw["first_response_due_by_time"])
  created = ms(raw["created_time"]); updated = ms(raw["last_updated_time"])
  responded = ms(raw["responded_time"]); tech = nm(raw["technician"])
  paused = !!st["stop_timer"]

  reasons = []; risks = []
  reasons << "Resolution SLA breached" if raw["is_overdue"]
  reasons << "First response SLA breached" if raw["is_first_response_overdue"]
  risk_ms = CFG[:at_risk_hours] * 3_600_000
  if reasons.empty? && !paused
    risks << "Resolution due in < #{g(CFG[:at_risk_hours])}h" if due && due - now <= risk_ms
    risks << "First response due in < #{g(CFG[:at_risk_hours])}h" if fr_due && !responded && fr_due - now <= risk_ms
  end
  risks << "Unassigned" unless tech
  risks << "No SLA due date" if !due && !paused
  last = updated || created
  risks << "No update in #{g(CFG[:stale_hours])}h+" if !paused && last && now - last > CFG[:stale_hours] * 3_600_000

  {
    id: raw["id"], display_id: raw["display_id"] || raw["id"], subject: raw["subject"] || "(no subject)",
    priority: nm(raw["priority"]) || "None", priority_color: (raw["priority"] || {})["color"],
    status: status, sla_paused: paused, level: nm(raw["level"]), request_type: nm(raw["request_type"]),
    technician: tech, group: nm(raw["group"]), requester: nm(raw["requester"]), site: nm(raw["site"]),
    created: created, due: due, first_response_due: fr_due, last_updated: updated,
    health: reasons.any? ? "breached" : risks.any? ? "at_risk" : "healthy",
    issues: reasons + risks, url: ticket_link(raw["id"]),
    _in_progress: st["in_progress"],
  }
end

def build_payload(raw)
  now = (Time.now.to_f * 1000).to_i
  pri_set = CFG[:priorities].map(&:downcase)
  lvl_set = CFG[:levels].map(&:downcase)
  order = { "breached" => 0, "at_risk" => 1, "healthy" => 2 }
  tickets = raw.map { |r| classify(r, now) }.select do |t|
    pri_set.include?(t[:priority].downcase) &&
      (lvl_set.empty? || lvl_set.include?(t[:level].to_s.downcase)) && t[:_in_progress] != false && !CFG[:closed].include?(t[:status].downcase)
  end
  tickets.each { |t| t.delete(:_in_progress) }
  tickets.sort_by! { |t| [order[t[:health]], pri_set.index(t[:priority].downcase) || 99, t[:due] || 2**62] }
  { source: "live", fetched_at: now,
    config: { priorities: CFG[:priorities], levels: CFG[:levels], at_risk_hours: CFG[:at_risk_hours],
              stale_hours: CFG[:stale_hours], refresh_seconds: CFG[:refresh_seconds] },
    tickets: tickets }
end

# ------------------------------------------------------------------ cache + server
class Cache
  def initialize
    @client = SDPClient.new
    @payload = nil; @error = nil; @fetched = 0
    @lock = Mutex.new
  end

  def get(force)
    @lock.synchronize do
      fresh = Time.now.to_f - @fetched < [15, CFG[:refresh_seconds] - 5].max
      return [@payload, @error] if @payload && fresh && !force
      begin
        @payload = build_payload(@client.open_critical_requests)
        @error = nil
      rescue => e
        @error = e.message
        warn "[#{Time.now.strftime("%H:%M:%S")}] fetch failed: #{e.message}"
      end
      @fetched = Time.now.to_f
      [@payload, @error]
    end
  end

  def status
    { ok: !@payload.nil? && @error.nil?, last_fetch: @fetched.zero? ? nil : Time.at(@fetched).utc.iso8601,
      tickets: @payload ? @payload[:tickets].size : nil, error: @error }
  end
end

require "time"
CACHE = Cache.new

# Constant-time comparison so the password can't be guessed byte by byte from response timing
def secure_eq(a, b)
  return false unless a.bytesize == b.bytesize
  a.bytes.zip(b.bytes).reduce(0) { |acc, (x, y)| acc | (x ^ y) }.zero?
end

auth = lambda do |req, res|
  next if CFG[:auth_password].empty? || req.path == "/healthz"
  WEBrick::HTTPAuth.basic_auth(req, res, "Escalations Dashboard") do |user, pass|
    secure_eq(user.to_s, CFG[:auth_user]) & secure_eq(pass.to_s, CFG[:auth_password])
  end
end

server = WEBrick::HTTPServer.new(BindAddress: CFG[:host], Port: CFG[:port],
                                 DocumentRoot: File.join(ROOT, "public"),
                                 RequestCallback: auth,
                                 AccessLog: [], Logger: WEBrick::Log.new($stderr, WEBrick::Log::WARN))
server.mount_proc("/healthz") do |_, res|
  s = CACHE.status
  res.status = s[:ok] || s[:last_fetch].nil? ? 200 : 503
  res["Content-Type"] = "application/json"
  res.body = s.to_json
end
server.mount_proc("/api/tickets") do |req, res|
  payload, err = CACHE.get(req.query_string.to_s.include?("force=1"))
  body = (payload || { tickets: [], source: "none", fetched_at: nil,
                       config: { priorities: CFG[:priorities], refresh_seconds: CFG[:refresh_seconds] } }).dup
  body[:error] = err
  res["Content-Type"] = "application/json"
  res["Cache-Control"] = "no-store"
  res.body = body.to_json
end
trap("INT") { server.shutdown }
trap("TERM") { server.shutdown }
puts "Escalations Dashboard - LIVE (#{CFG[:base_url]})"
puts "Tracking priorities: #{CFG[:priorities].join(", ")}"
puts "Open http://localhost:#{CFG[:port]}"
$stdout.flush
server.start
