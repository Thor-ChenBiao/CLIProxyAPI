package executor

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"

	"github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/router-for-me/CLIProxyAPI/v6/internal/config"
	"github.com/router-for-me/CLIProxyAPI/v6/internal/runtime/executor/helps"
	"github.com/router-for-me/CLIProxyAPI/v6/internal/thinking"
	cliproxyauth "github.com/router-for-me/CLIProxyAPI/v6/sdk/cliproxy/auth"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v6/sdk/cliproxy/executor"
	sdktranslator "github.com/router-for-me/CLIProxyAPI/v6/sdk/translator"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

const bedrockClaudeProvider = "bedrock-claude"

type BedrockClaudeExecutor struct {
	cfg *config.Config
}

func NewBedrockClaudeExecutor(cfg *config.Config) *BedrockClaudeExecutor {
	return &BedrockClaudeExecutor{cfg: cfg}
}

func (e *BedrockClaudeExecutor) Identifier() string { return bedrockClaudeProvider }

func (e *BedrockClaudeExecutor) Execute(ctx context.Context, auth *cliproxyauth.Auth, req cliproxyexecutor.Request, opts cliproxyexecutor.Options) (resp cliproxyexecutor.Response, err error) {
	if opts.Alt == "responses/compact" {
		return resp, statusErr{code: http.StatusNotImplemented, msg: "/responses/compact not supported"}
	}
	baseModel := thinking.ParseSuffix(req.Model).ModelName
	bedrockModelID := resolveBedrockClaudeModelID(auth, baseModel)
	if bedrockModelID == "" {
		return resp, statusErr{code: http.StatusBadGateway, msg: fmt.Sprintf("bedrock model mapping not found for %s", baseModel)}
	}

	reporter := helps.NewUsageReporter(ctx, e.Identifier(), baseModel, auth)
	defer reporter.TrackFailure(ctx, &err)

	from := opts.SourceFormat
	to := sdktranslator.FromString("claude")
	streamTranslation := false
	originalPayloadSource := req.Payload
	if len(opts.OriginalRequest) > 0 {
		originalPayloadSource = opts.OriginalRequest
	}
	originalTranslated := sdktranslator.TranslateRequest(from, to, baseModel, originalPayloadSource, streamTranslation)
	body := sdktranslator.TranslateRequest(from, to, baseModel, req.Payload, streamTranslation)
	body, _ = sjson.SetBytes(body, "model", baseModel)
	body, err = thinking.ApplyThinking(body, req.Model, from.String(), to.String(), e.Identifier())
	if err != nil {
		return resp, err
	}
	requestedModel := helps.PayloadRequestedModel(opts, req.Model)
	requestPath := helps.PayloadRequestPath(opts)
	body = helps.ApplyPayloadConfigWithRoot(e.cfg, baseModel, to.String(), "", body, originalTranslated, requestedModel, requestPath)
	body = ensureModelMaxTokens(body, baseModel)
	body = disableThinkingIfToolChoiceForced(body)
	body = normalizeClaudeTemperatureForThinking(body)
	if countCacheControls(body) == 0 {
		body = ensureCacheControl(body)
	}
	body = enforceCacheControlLimit(body, 4)
	body = normalizeCacheControlTTL(body)
	bodyForTranslation := body
	bodyForUpstream := prepareBedrockClaudeBody(body)

	awsCfg, err := loadBedrockAWSConfig(ctx, auth)
	if err != nil {
		return resp, err
	}
	client := bedrockruntime.NewFromConfig(awsCfg)
	input := &bedrockruntime.InvokeModelInput{
		ModelId:     aws.String(bedrockModelID),
		ContentType: aws.String("application/json"),
		Accept:      aws.String("application/json"),
		Body:        bodyForUpstream,
	}

	var authID, authLabel string
	if auth != nil {
		authID = auth.ID
		authLabel = auth.Label
	}
	region := ""
	if auth != nil && auth.Attributes != nil {
		region = strings.TrimSpace(auth.Attributes["region"])
	}
	helps.RecordAPIRequest(ctx, e.cfg, helps.UpstreamRequestLog{
		URL:       fmt.Sprintf("bedrock-runtime:%s:%s", region, bedrockModelID),
		Method:    http.MethodPost,
		Headers:   http.Header{"Content-Type": []string{"application/json"}, "Accept": []string{"application/json"}},
		Body:      bodyForUpstream,
		Provider:  e.Identifier(),
		AuthID:    authID,
		AuthLabel: authLabel,
		AuthType:  "aws-profile",
	})

	out, err := client.InvokeModel(ctx, input)
	if err != nil {
		helps.RecordAPIResponseError(ctx, e.cfg, err)
		return resp, err
	}
	data := out.Body
	helps.RecordAPIResponseMetadata(ctx, e.cfg, http.StatusOK, http.Header{"Content-Type": []string{"application/json"}})
	helps.AppendAPIResponseChunk(ctx, e.cfg, data)
	if streamTranslation {
		if errValidate := validateClaudeStreamingResponse(data); errValidate != nil {
			helps.RecordAPIResponseError(ctx, e.cfg, errValidate)
			return resp, errValidate
		}
		for _, line := range strings.Split(string(data), "\n") {
			if detail, ok := helps.ParseClaudeStreamUsage([]byte(line)); ok {
				reporter.Publish(ctx, detail)
			}
		}
	} else {
		reporter.Publish(ctx, helps.ParseClaudeUsage(data))
	}
	dataForTranslation := data
	if !streamTranslation {
		dataForTranslation = bedrockClaudeResponseForTranslation(data)
	}
	var param any
	translated := sdktranslator.TranslateNonStream(ctx, to, from, req.Model, opts.OriginalRequest, bodyForTranslation, dataForTranslation, &param)
	return cliproxyexecutor.Response{Payload: translated, Headers: http.Header{"Content-Type": []string{"application/json"}}}, nil
}

func (e *BedrockClaudeExecutor) ExecuteStream(ctx context.Context, auth *cliproxyauth.Auth, req cliproxyexecutor.Request, opts cliproxyexecutor.Options) (*cliproxyexecutor.StreamResult, error) {
	return nil, statusErr{code: http.StatusNotImplemented, msg: "bedrock claude streaming is not implemented yet"}
}

func (e *BedrockClaudeExecutor) Refresh(ctx context.Context, auth *cliproxyauth.Auth) (*cliproxyauth.Auth, error) {
	return auth, nil
}

func (e *BedrockClaudeExecutor) CountTokens(ctx context.Context, auth *cliproxyauth.Auth, req cliproxyexecutor.Request, opts cliproxyexecutor.Options) (cliproxyexecutor.Response, error) {
	return cliproxyexecutor.Response{}, statusErr{code: http.StatusNotImplemented, msg: "bedrock claude token counting is not implemented yet"}
}

func (e *BedrockClaudeExecutor) HttpRequest(ctx context.Context, auth *cliproxyauth.Auth, req *http.Request) (*http.Response, error) {
	return nil, statusErr{code: http.StatusNotImplemented, msg: "bedrock claude raw HTTP requests are not supported"}
}

func loadBedrockAWSConfig(ctx context.Context, auth *cliproxyauth.Auth) (aws.Config, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	var opts []func(*awsconfig.LoadOptions) error
	if auth != nil && auth.Attributes != nil {
		if region := strings.TrimSpace(auth.Attributes["region"]); region != "" {
			opts = append(opts, awsconfig.WithRegion(region))
		}
		if profile := strings.TrimSpace(auth.Attributes["profile"]); profile != "" {
			opts = append(opts, awsconfig.WithSharedConfigProfile(profile))
		}
	}
	cfg, err := awsconfig.LoadDefaultConfig(ctx, opts...)
	if err != nil {
		return aws.Config{}, fmt.Errorf("load bedrock aws config: %w", err)
	}
	if strings.TrimSpace(cfg.Region) == "" {
		return aws.Config{}, fmt.Errorf("bedrock region is required")
	}
	return cfg, nil
}

func resolveBedrockClaudeModelID(auth *cliproxyauth.Auth, model string) string {
	model = strings.TrimSpace(model)
	if auth == nil || auth.Attributes == nil || model == "" {
		return ""
	}
	raw := strings.TrimSpace(auth.Attributes["bedrock_models"])
	if raw == "" {
		return ""
	}
	var mappings []config.BedrockClaudeModel
	if err := json.Unmarshal([]byte(raw), &mappings); err != nil {
		return ""
	}
	for _, mapping := range mappings {
		name := strings.TrimSpace(mapping.Name)
		alias := strings.TrimSpace(mapping.Alias)
		modelID := strings.TrimSpace(mapping.BedrockModelID)
		if modelID == "" {
			continue
		}
		if strings.EqualFold(model, name) || strings.EqualFold(model, alias) || strings.EqualFold(model, modelID) {
			return modelID
		}
	}
	return ""
}

func prepareBedrockClaudeBody(body []byte) []byte {
	if !gjson.GetBytes(body, "anthropic_version").Exists() {
		body, _ = sjson.SetBytes(body, "anthropic_version", "bedrock-2023-05-31")
	}
	body, _ = sjson.DeleteBytes(body, "model")
	body, _ = sjson.DeleteBytes(body, "stream")
	return body
}

func bedrockClaudeResponseForTranslation(data []byte) []byte {
	root := gjson.ParseBytes(data)
	if root.Get("type").String() != "message" || !root.Get("content").IsArray() {
		return data
	}

	var b strings.Builder
	appendEvent := func(event map[string]any) {
		payload, err := json.Marshal(event)
		if err != nil {
			return
		}
		b.WriteString("data: ")
		b.Write(payload)
		b.WriteByte('\n')
	}

	appendEvent(map[string]any{
		"type":    "message_start",
		"message": json.RawMessage(data),
	})

	root.Get("content").ForEach(func(key, value gjson.Result) bool {
		index := int(key.Int())
		switch value.Get("type").String() {
		case "text":
			appendEvent(map[string]any{
				"type":  "content_block_delta",
				"index": index,
				"delta": map[string]any{"type": "text_delta", "text": value.Get("text").String()},
			})
		case "thinking":
			appendEvent(map[string]any{
				"type":  "content_block_delta",
				"index": index,
				"delta": map[string]any{"type": "thinking_delta", "thinking": value.Get("thinking").String()},
			})
		case "tool_use":
			appendEvent(map[string]any{
				"type":          "content_block_start",
				"index":         index,
				"content_block": json.RawMessage(value.Raw),
			})
			if input := value.Get("input"); input.Exists() {
				appendEvent(map[string]any{
					"type":  "content_block_delta",
					"index": index,
					"delta": map[string]any{"type": "input_json_delta", "partial_json": input.Raw},
				})
			}
		}
		appendEvent(map[string]any{"type": "content_block_stop", "index": index})
		return true
	})

	messageDelta := map[string]any{
		"type":  "message_delta",
		"delta": map[string]any{"stop_reason": root.Get("stop_reason").String()},
	}
	if usage := root.Get("usage"); usage.Exists() {
		messageDelta["usage"] = json.RawMessage(usage.Raw)
	}
	appendEvent(messageDelta)
	appendEvent(map[string]any{"type": "message_stop"})
	return []byte(b.String())
}
