"""Structured content for Key Portal help pages."""


def claude_code_command(base_url):
    return f"""node <<'CLAUDE_EOF'
const fs=require('fs'),path=require('path'),os=require('os');
const apiKey='<YOUR_API_KEY>';
const baseUrl='{base_url}';
const dir=path.join(os.homedir(),'.claude');
const sf=path.join(dir,'settings.json');
const cf=path.join(os.homedir(),'.claude.json');
if(!fs.existsSync(dir))fs.mkdirSync(dir,{{recursive:true}});
const s=fs.existsSync(sf)?JSON.parse(fs.readFileSync(sf,'utf8')):{{}};
s.apiKeyHelper='echo '+apiKey;
s.env={{...(s.env||{{}}),ANTHROPIC_BASE_URL:baseUrl}};
fs.writeFileSync(sf,JSON.stringify(s,null,2));
const c=fs.existsSync(cf)?JSON.parse(fs.readFileSync(cf,'utf8')):{{}};
c.hasCompletedOnboarding=true;
fs.writeFileSync(cf,JSON.stringify(c,null,2));
console.log('Claude Code config done. Replace <YOUR_API_KEY> before running.');
CLAUDE_EOF"""


def codex_command(base_url):
    return f"""node <<'CODEX_EOF'
const fs=require('fs'),path=require('path'),os=require('os');
const apiKey='<YOUR_API_KEY>';
const dir=path.join(os.homedir(),'.codex');
const sf=path.join(dir,'config.toml');
if(!fs.existsSync(dir))fs.mkdirSync(dir,{{recursive:true}});
let c=fs.existsSync(sf)?fs.readFileSync(sf,'utf8'):'';
function upsert(key,value){{
  const line=key+' = '+value;
  const re=new RegExp('^'+key+'\\\\s*=.*$','m');
  c=re.test(c)?c.replace(re,line):line+'\\n'+c;
}}
upsert('model_provider','"local"');
upsert('model','"gpt-5.5"');
upsert('model_reasoning_effort','"high"');
c=c.replace(/^openai_base_url\\s*=.*\\n?/m,'');
const provider=[
  '[model_providers.local]',
  'name = "local"',
  'base_url = "{base_url}/v1"',
  'env_key = "OPENAI_API_KEY"',
  'wire_api = "responses"'
].join('\\n');
c=/\\[model_providers\\.local\\][\\s\\S]*?(?=\\n\\[|$)/m.test(c)
  ? c.replace(/\\[model_providers\\.local\\][\\s\\S]*?(?=\\n\\[|$)/m,provider)
  : c.trimEnd()+'\\n\\n'+provider;
fs.writeFileSync(sf,c.trimEnd()+'\\n');
console.log('Codex config done: '+sf);
console.log('Then export OPENAI_API_KEY='+apiKey);
CODEX_EOF
export OPENAI_API_KEY='<YOUR_API_KEY>'"""


def guide_context(base_url):
    return {
        "base_url": base_url,
        "models": [
            {
                "name": "GPT Key",
                "key_type": "common",
                "approval": "无需审批",
                "models": "gpt-5.5，以及 GPT-backed 的 claude-* 公开模型名",
                "recommendation": "默认推荐。Claude Code、Codex、日常开发优先用它。",
            },
            {
                "name": "Claude Key",
                "key_type": "claude",
                "approval": "需要审批",
                "models": "claude-opus-4-8、claude-opus-4-7、claude-opus-4-6、claude-sonnet-4-6、claude-sonnet-4-5、claude-haiku-4-5",
                "recommendation": "只有 GPT Key 明显不满足、且必须走真实 Claude/Bedrock 时再申请。",
            },
            {
                "name": "DeepSeek Key",
                "key_type": "deepseek",
                "approval": "需要审批",
                "models": "deepseek-chat、deepseek-reasoner",
                "recommendation": "用于明确需要 DeepSeek V3/R1 的任务。",
            },
        ],
        "commands": [
            {
                "id": "claude-code",
                "title": "Claude Code",
                "note": "把 <YOUR_API_KEY> 替换成你在“我的 Keys”里复制的 Key。",
                "command": claude_code_command(base_url),
            },
            {
                "id": "codex-cli",
                "title": "Codex CLI",
                "note": "同一个 Key 可以给 Codex CLI 使用，默认模型建议 gpt-5.5。",
                "command": codex_command(base_url),
            },
        ],
        "links": [
            {"label": "CC Switch", "href": "https://github.com/farion1231/cc-switch", "note": "多 Key、多配置切换推荐使用。"},
            {"label": "公司 Claude Skills", "href": "https://github.com/zilliztech/cloud-skills", "note": "Claude Code 能力包。"},
        ],
    }
