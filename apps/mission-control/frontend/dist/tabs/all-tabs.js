// Barrel: all 19 S7 tab modules re-exported for tests and dynamic import.
// Each module exposes ROUTE, LABEL, GROUP, READ_ONLY_NOTE, SOURCE_ENDPOINTS
// and pure render helpers.

export { renderIssuesList, renderIssueDetail } from './issues.js';
export { renderPermitsList, renderPermitDetail } from './permits.js';
export { renderRoomBinding } from './room-binding.js';
export { renderAudit } from './action-audit.js';
export { renderSkills } from './skills.js';
export { renderMemory, renderMemorySearch } from './memory.js';
export { renderProfiles } from './profiles.js';
export { renderModelInfo, renderModelOptions } from './models.js';
export { renderToolsets } from './tools.js';
export { renderMcpServers } from './mcp.js';
export { renderPlugins } from './plugins.js';
export { renderWebhooks } from './webhooks.js';
export { renderChannels } from './channels.js';
export { renderArtifacts, renderTaskAttachments } from './artifacts.js';
export { renderFiles, assertSafePath } from './files.js';
export { renderLogs, boundedLines } from './logs.js';
export { commandCenterCapabilities, actionBadges } from './command-center.js';
export { renderConfig, redactConfig } from './settings.js';
export { createLlamaProxyManager, diagnosticsStrip, HEADER_PROBE, LLaMA_PROXY_URL, LLaMA_PROXY_MODE } from './llama-proxy.js';
