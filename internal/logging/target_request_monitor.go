package logging

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v7/internal/util"
	log "github.com/sirupsen/logrus"
	"gopkg.in/yaml.v3"
)

// TargetRequestMonitorMiddleware is a deliberately self-contained sidecar monitor.
// It is enabled only by a sidecar YAML file and can be removed by deleting this file plus the one engine.Use call.
func TargetRequestMonitorMiddleware(configPath string) gin.HandlerFunc {
	monitor := newTargetRequestMonitor(configPath)
	return func(c *gin.Context) {
		if monitor == nil {
			c.Next()
			return
		}

		target, matchKind, matchValue := monitor.match(c.Request)
		if target == nil {
			c.Next()
			return
		}

		limitBytes := target.maxFileBytes()
		if c.Request.ContentLength > limitBytes {
			// Too large means skip this monitoring copy. The proxied request must continue normally.
			target.drop("request_too_large", nil, int64(c.Request.ContentLength), 0)
			c.Next()
			return
		}

		requestBody, tooLarge, err := readLimitedTargetBody(c.Request.Body, limitBytes)
		if err != nil {
			target.drop("read_request_failed", nil, 0, 0)
			c.Next()
			return
		}
		if tooLarge {
			target.drop("request_too_large", nil, limitBytes+1, 0)
			c.Request.Body = io.NopCloser(io.MultiReader(bytes.NewReader(requestBody), c.Request.Body))
			c.Next()
			return
		}
		c.Request.Body = io.NopCloser(bytes.NewReader(requestBody))

		writer := &targetMonitorResponseWriter{
			ResponseWriter: c.Writer,
			limitBytes:     limitBytes,
			body:           bytes.NewBuffer(nil),
		}
		c.Writer = writer

		started := time.Now()
		c.Next()

		if writer.tooLarge.Load() {
			target.drop("response_too_large", c, int64(len(requestBody)), int64(writer.totalBytes.Load()))
			return
		}

		record := targetMonitorRecord{
			StartedAt:       started,
			Method:          c.Request.Method,
			URL:             requestURL(c.Request),
			RequestHeaders:  cloneHeader(c.Request.Header),
			RequestBody:     append([]byte(nil), requestBody...),
			Status:          writer.status(),
			ResponseHeaders: cloneHeader(writer.Header()),
			ResponseBody:    append([]byte(nil), writer.body.Bytes()...),
			RequestID:       GetGinRequestID(c),
			RemoteAddr:      c.ClientIP(),
			UserAgent:       c.Request.UserAgent(),
			MatchKind:       matchKind,
			MatchValue:      matchValue,
		}

		// The monitor queue is intentionally lossy: full queue drops logs instead of blocking proxy traffic.
		select {
		case target.queue <- record:
		default:
			target.drop("queue_full", c, int64(len(requestBody)), int64(writer.totalBytes.Load()))
		}
	}
}

type targetMonitorConfig struct {
	Enabled   bool                  `yaml:"enabled"`
	OutputDir string                `yaml:"output-dir"`
	Targets   []targetMonitorTarget `yaml:"targets"`
}

type targetMonitorTarget struct {
	Label                 string   `yaml:"label"`
	Key                   string   `yaml:"key"`
	KeySuffix             string   `yaml:"key-suffix"`
	LiteLLMUserID         string   `yaml:"litellm-user-id"`
	LiteLLMKeyAlias       string   `yaml:"litellm-key-alias"`
	LiteLLMKeyAliasSuffix string   `yaml:"litellm-key-alias-suffix"`
	HeaderMatches         []string `yaml:"header-matches"`
	StartAt               string   `yaml:"start-at"`
	StopAt                string   `yaml:"stop-at"`
	MaxTotalGB            int      `yaml:"max-total-gb"`
	MaxDailyGB            int      `yaml:"max-daily-gb"`
	MaxFileMB             int      `yaml:"max-file-mb"`
	MaxQueue              int      `yaml:"max-queue"`
	MinFreeDiskGB         int      `yaml:"min-free-disk-gb"`
}

type targetRequestMonitor struct {
	targets []*targetRequestMonitorTarget
}

type targetRequestMonitorTarget struct {
	cfg          targetMonitorTarget
	baseDir      string
	rawDir       string
	indexPath    string
	statePath    string
	droppedPath  string
	queue        chan targetMonitorRecord
	active       atomic.Bool
	droppedCount atomic.Uint64
	writtenBytes atomic.Uint64
	stopOnce     sync.Once
}

type targetMonitorRecord struct {
	StartedAt       time.Time
	Method          string
	URL             string
	RequestHeaders  http.Header
	RequestBody     []byte
	Status          int
	ResponseHeaders http.Header
	ResponseBody    []byte
	RequestID       string
	RemoteAddr      string
	UserAgent       string
	MatchKind       string
	MatchValue      string
}

type targetMonitorResponseWriter struct {
	gin.ResponseWriter
	body       *bytes.Buffer
	limitBytes int64
	totalBytes atomic.Int64
	tooLarge   atomic.Bool
	statusCode int
}

var targetMonitorPathCleaner = regexp.MustCompile(`[^A-Za-z0-9._-]+`)

func newTargetRequestMonitor(configPath string) *targetRequestMonitor {
	path := strings.TrimSpace(os.Getenv("TARGET_REQUEST_MONITOR_CONFIG"))
	if path == "" && configPath != "" {
		path = configPath + ".target-monitor.yaml"
	}
	if path == "" {
		return nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var cfg targetMonitorConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		log.WithError(err).Warn("target request monitor config ignored")
		return nil
	}
	if !cfg.Enabled || len(cfg.Targets) == 0 {
		return nil
	}

	outputDir := strings.TrimSpace(cfg.OutputDir)
	if outputDir == "" {
		outputDir = "target-monitor-logs"
	}
	if !filepath.IsAbs(outputDir) && configPath != "" {
		outputDir = filepath.Join(filepath.Dir(configPath), outputDir)
	}

	monitor := &targetRequestMonitor{}
	for _, item := range cfg.Targets {
		if strings.TrimSpace(item.Label) == "" {
			item.Label = "target"
		}
		if !item.hasMatcher() {
			continue
		}
		if item.MaxQueue <= 0 {
			item.MaxQueue = 200
		}
		targetDir := filepath.Join(outputDir, safeTargetSegment(item.Label))
		target := &targetRequestMonitorTarget{
			cfg:         item,
			baseDir:     targetDir,
			rawDir:      filepath.Join(targetDir, "raw"),
			indexPath:   filepath.Join(targetDir, "index.jsonl"),
			statePath:   filepath.Join(targetDir, "monitor-state.json"),
			droppedPath: filepath.Join(targetDir, "dropped.jsonl"),
			queue:       make(chan targetMonitorRecord, item.MaxQueue),
		}
		target.active.Store(true)
		if err := os.MkdirAll(target.rawDir, 0750); err != nil {
			log.WithError(err).Warn("target request monitor target disabled")
			continue
		}
		target.writeState(true, "")
		go target.worker()
		monitor.targets = append(monitor.targets, target)
	}
	if len(monitor.targets) == 0 {
		return nil
	}
	return monitor
}

func (m *targetRequestMonitor) match(req *http.Request) (*targetRequestMonitorTarget, string, string) {
	if m == nil || req == nil {
		return nil, "", ""
	}
	now := time.Now()
	for _, target := range m.targets {
		if target == nil || !target.active.Load() || !target.inWindow(now) {
			continue
		}
		if kind, value := target.cfg.match(req); kind != "" {
			return target, kind, value
		}
	}
	return nil, "", ""
}

func (cfg targetMonitorTarget) hasMatcher() bool {
	return firstTargetText(cfg.Key, cfg.KeySuffix, cfg.LiteLLMUserID, cfg.LiteLLMKeyAlias, cfg.LiteLLMKeyAliasSuffix) != "" || len(cfg.HeaderMatches) > 0
}

func (cfg targetMonitorTarget) match(req *http.Request) (string, string) {
	key := downstreamKey(req)
	if cfg.Key != "" && key == strings.TrimSpace(cfg.Key) {
		return "key", key
	}
	if cfg.KeySuffix != "" && key != "" && strings.HasSuffix(key, strings.TrimSpace(cfg.KeySuffix)) {
		return "key-suffix", key
	}
	if value := strings.TrimSpace(req.Header.Get("X-Litellm-User_api_key_user_id")); cfg.LiteLLMUserID != "" && value == strings.TrimSpace(cfg.LiteLLMUserID) {
		return "litellm-user-id", value
	}
	if value := strings.TrimSpace(req.Header.Get("X-Litellm-User_api_key_alias")); cfg.LiteLLMKeyAlias != "" && value == strings.TrimSpace(cfg.LiteLLMKeyAlias) {
		return "litellm-key-alias", value
	}
	if value := strings.TrimSpace(req.Header.Get("X-Litellm-User_api_key_alias")); cfg.LiteLLMKeyAliasSuffix != "" && strings.HasSuffix(value, strings.TrimSpace(cfg.LiteLLMKeyAliasSuffix)) {
		return "litellm-key-alias-suffix", value
	}
	for _, item := range cfg.HeaderMatches {
		header, want, ok := strings.Cut(item, ":")
		if !ok {
			continue
		}
		value := strings.TrimSpace(req.Header.Get(strings.TrimSpace(header)))
		want = strings.TrimSpace(want)
		if value == want || strings.HasSuffix(value, want) {
			return "header:" + strings.TrimSpace(header), value
		}
	}
	return "", ""
}

func (t *targetRequestMonitorTarget) worker() {
	for record := range t.queue {
		if !t.active.Load() {
			continue
		}
		if reason := t.stopReason(record); reason != "" {
			t.drop(reason, nil, int64(len(record.RequestBody)), int64(len(record.ResponseBody)))
			t.stop(reason)
			continue
		}
		if err := t.write(record); err != nil {
			log.WithError(err).Warn("target request monitor write failed")
			t.drop("write_failed", nil, int64(len(record.RequestBody)), int64(len(record.ResponseBody)))
		}
	}
}

func (t *targetRequestMonitorTarget) stopReason(record targetMonitorRecord) string {
	if !t.inWindow(time.Now()) {
		return "time_window_closed"
	}
	if t.cfg.MinFreeDiskGB > 0 {
		free, err := freeBytes(t.baseDir)
		if err == nil && free < uint64(t.cfg.MinFreeDiskGB)*1024*1024*1024 {
			return "min_free_disk_gb_reached"
		}
	}
	if t.cfg.MaxTotalGB > 0 {
		used, err := dirBytes(t.baseDir)
		if err == nil && used >= int64(t.cfg.MaxTotalGB)*1024*1024*1024 {
			return "max_total_gb_reached"
		}
	}
	if t.cfg.MaxDailyGB > 0 {
		dayDir := filepath.Join(t.rawDir, record.StartedAt.UTC().Format("2006-01-02"))
		used, err := dirBytes(dayDir)
		if err == nil && used >= int64(t.cfg.MaxDailyGB)*1024*1024*1024 {
			return "max_daily_gb_reached"
		}
	}
	return ""
}

func (t *targetRequestMonitorTarget) write(record targetMonitorRecord) error {
	day := record.StartedAt.UTC().Format("2006-01-02")
	dayDir := filepath.Join(t.rawDir, day)
	if err := os.MkdirAll(dayDir, 0750); err != nil {
		return err
	}
	name := safeTargetSegment(fmt.Sprintf("%s-%s.log", record.StartedAt.UTC().Format("2006-01-02T150405Z"), firstTargetText(record.RequestID, fmt.Sprintf("%d", time.Now().UnixNano()))))
	path := filepath.Join(dayDir, name)
	if err := writeTargetLog(path, t.cfg, record); err != nil {
		return err
	}
	if info, err := os.Stat(path); err == nil {
		t.writtenBytes.Add(uint64(info.Size()))
	}
	entry := map[string]any{
		"ts":             record.StartedAt.UTC().Format(time.RFC3339),
		"label":          t.cfg.Label,
		"key_masked":     util.HideAPIKey(firstTargetText(t.cfg.Key, t.cfg.KeySuffix, t.cfg.LiteLLMKeyAlias, t.cfg.LiteLLMKeyAliasSuffix, t.cfg.LiteLLMUserID)),
		"match_kind":     record.MatchKind,
		"match_value":    util.HideAPIKey(record.MatchValue),
		"method":         record.Method,
		"url":            record.URL,
		"model":          targetModel(record.RequestBody),
		"status":         record.Status,
		"request_bytes":  len(record.RequestBody),
		"response_bytes": len(record.ResponseBody),
		"request_id":     record.RequestID,
		"remote_addr":    record.RemoteAddr,
		"raw_file":       relTargetPath(t.baseDir, path),
	}
	return appendTargetJSONL(t.indexPath, entry)
}

func writeTargetLog(path string, cfg targetMonitorTarget, record targetMonitorRecord) error {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	defer f.Close()
	w := bufio.NewWriter(f)
	defer w.Flush()
	fmt.Fprintf(w, "========== %s ==========\n", record.StartedAt.UTC().Format(time.RFC3339))
	fmt.Fprintf(w, "Label: %s\n", cfg.Label)
	fmt.Fprintf(w, "Key: %s\n", util.HideAPIKey(firstTargetText(cfg.Key, cfg.KeySuffix, cfg.LiteLLMKeyAlias, cfg.LiteLLMKeyAliasSuffix, cfg.LiteLLMUserID)))
	fmt.Fprintf(w, "Match: %s %s\n", record.MatchKind, util.HideAPIKey(record.MatchValue))
	fmt.Fprintf(w, "Method: %s\n", record.Method)
	fmt.Fprintf(w, "URL: %s\n", record.URL)
	fmt.Fprintf(w, "Status: %d\n", record.Status)
	fmt.Fprintf(w, "Request-ID: %s\n", record.RequestID)
	fmt.Fprintf(w, "Remote: %s\n", record.RemoteAddr)
	fmt.Fprintf(w, "User-Agent: %s\n", record.UserAgent)
	fmt.Fprintf(w, "Request-Size: %d\n", len(record.RequestBody))
	fmt.Fprintf(w, "Response-Size: %d\n", len(record.ResponseBody))
	fmt.Fprintf(w, "Model: %s\n", targetModel(record.RequestBody))
	writeTargetHeaders(w, "REQUEST HEADERS", record.RequestHeaders)
	writeTargetHeaders(w, "RESPONSE HEADERS", record.ResponseHeaders)
	writeTargetSection(w, "REQUEST BODY", record.RequestBody)
	writeTargetSection(w, "RESPONSE BODY", record.ResponseBody)
	return nil
}

func readLimitedTargetBody(body io.Reader, limitBytes int64) ([]byte, bool, error) {
	if body == nil {
		return nil, false, nil
	}
	reader := io.LimitReader(body, limitBytes+1)
	data, err := io.ReadAll(reader)
	if err != nil {
		return nil, false, err
	}
	if int64(len(data)) > limitBytes {
		return data[:limitBytes], true, nil
	}
	return data, false, nil
}

func (w *targetMonitorResponseWriter) WriteHeader(code int) {
	w.statusCode = code
	w.ResponseWriter.WriteHeader(code)
}

func (w *targetMonitorResponseWriter) Write(data []byte) (int, error) {
	n, err := w.ResponseWriter.Write(data)
	w.capture(data)
	return n, err
}

func (w *targetMonitorResponseWriter) WriteString(data string) (int, error) {
	n, err := w.ResponseWriter.WriteString(data)
	w.capture([]byte(data))
	return n, err
}

func (w *targetMonitorResponseWriter) status() int {
	if w.statusCode != 0 {
		return w.statusCode
	}
	if statusWriter, ok := w.ResponseWriter.(interface{ Status() int }); ok {
		return statusWriter.Status()
	}
	return http.StatusOK
}

func (w *targetMonitorResponseWriter) capture(data []byte) {
	if len(data) == 0 || w.tooLarge.Load() {
		return
	}
	newTotal := w.totalBytes.Add(int64(len(data)))
	if newTotal > w.limitBytes {
		w.tooLarge.Store(true)
		w.body.Reset()
		return
	}
	_, _ = w.body.Write(data)
}

func (t *targetRequestMonitorTarget) maxFileBytes() int64 {
	if t.cfg.MaxFileMB <= 0 {
		return 256 * 1024 * 1024
	}
	return int64(t.cfg.MaxFileMB) * 1024 * 1024
}

func (t *targetRequestMonitorTarget) inWindow(now time.Time) bool {
	if strings.TrimSpace(t.cfg.StartAt) != "" {
		start, err := time.Parse(time.RFC3339, t.cfg.StartAt)
		if err == nil && now.Before(start) {
			return false
		}
	}
	if strings.TrimSpace(t.cfg.StopAt) != "" {
		stop, err := time.Parse(time.RFC3339, t.cfg.StopAt)
		if err == nil && !now.Before(stop) {
			t.stop("stop_at_reached")
			return false
		}
	}
	return true
}

func (t *targetRequestMonitorTarget) stop(reason string) {
	t.stopOnce.Do(func() {
		t.active.Store(false)
		t.writeState(false, reason)
	})
}

func (t *targetRequestMonitorTarget) writeState(enabled bool, reason string) {
	state := map[string]any{
		"enabled":       enabled,
		"label":         t.cfg.Label,
		"written_bytes": t.writtenBytes.Load(),
		"dropped_count": t.droppedCount.Load(),
	}
	if enabled {
		state["started_at"] = time.Now().UTC().Format(time.RFC3339)
	} else {
		state["stopped_at"] = time.Now().UTC().Format(time.RFC3339)
		state["reason"] = reason
	}
	_ = writeTargetJSON(t.statePath, state)
}

func (t *targetRequestMonitorTarget) drop(reason string, c *gin.Context, reqBytes, respBytes int64) {
	t.droppedCount.Add(1)
	entry := map[string]any{
		"ts":             time.Now().UTC().Format(time.RFC3339),
		"label":          t.cfg.Label,
		"reason":         reason,
		"request_bytes":  reqBytes,
		"response_bytes": respBytes,
	}
	if c != nil {
		entry["url"] = requestURL(c.Request)
		entry["request_id"] = GetGinRequestID(c)
	}
	_ = appendTargetJSONL(t.droppedPath, entry)
}

func downstreamKey(req *http.Request) string {
	for _, header := range []string{"Authorization", "X-Api-Key", "X-Goog-Api-Key"} {
		value := strings.TrimSpace(req.Header.Get(header))
		if value == "" {
			continue
		}
		if strings.EqualFold(header, "Authorization") {
			parts := strings.SplitN(value, " ", 2)
			if len(parts) == 2 && strings.EqualFold(parts[0], "Bearer") {
				return strings.TrimSpace(parts[1])
			}
		}
		return value
	}
	return ""
}

func requestURL(req *http.Request) string {
	if req == nil || req.URL == nil {
		return ""
	}
	out := req.URL.Path
	if req.URL.RawQuery != "" {
		out += "?" + util.MaskSensitiveQuery(req.URL.RawQuery)
	}
	return out
}

func writeTargetHeaders(w *bufio.Writer, title string, headers http.Header) {
	fmt.Fprintf(w, "--- %s ---\n", title)
	for key, values := range headers {
		for _, value := range values {
			fmt.Fprintf(w, "%s: %s\n", key, util.MaskSensitiveHeaderValue(key, value))
		}
	}
}

func writeTargetSection(w *bufio.Writer, title string, data []byte) {
	fmt.Fprintf(w, "--- %s ---\n", title)
	if len(data) == 0 {
		return
	}
	_, _ = w.Write(data)
	if data[len(data)-1] != '\n' {
		_, _ = w.WriteString("\n")
	}
}

func cloneHeader(headers http.Header) http.Header {
	out := make(http.Header, len(headers))
	for key, values := range headers {
		out[key] = append([]string(nil), values...)
	}
	return out
}

func targetModel(body []byte) string {
	var payload struct {
		Model string `json:"model"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return ""
	}
	return payload.Model
}

func safeTargetSegment(value string) string {
	value = strings.Trim(targetMonitorPathCleaner.ReplaceAllString(value, "-"), "-.")
	if value == "" {
		return "target"
	}
	if len(value) > 96 {
		return value[:96]
	}
	return value
}

func dirBytes(path string) (int64, error) {
	var total int64
	err := filepath.WalkDir(path, func(_ string, d os.DirEntry, err error) error {
		if err != nil || d == nil || d.IsDir() {
			return nil
		}
		if info, err := d.Info(); err == nil {
			total += info.Size()
		}
		return nil
	})
	return total, err
}

func freeBytes(path string) (uint64, error) {
	_ = os.MkdirAll(path, 0750)
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return 0, err
	}
	return stat.Bavail * uint64(stat.Bsize), nil
}

func appendTargetJSONL(path string, value any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0750); err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		return err
	}
	defer f.Close()
	line, err := json.Marshal(value)
	if err != nil {
		return err
	}
	_, err = f.Write(append(line, '\n'))
	return err
}

func writeTargetJSON(path string, value any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0750); err != nil {
		return err
	}
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(data, '\n'), 0600)
}

func relTargetPath(base, path string) string {
	if rel, err := filepath.Rel(base, path); err == nil {
		return rel
	}
	return path
}

func firstTargetText(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}
