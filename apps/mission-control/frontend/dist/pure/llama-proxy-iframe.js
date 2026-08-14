// llama-proxy iframe lifecycle (pure logic, DOM via injected document).
// Loaded directly so dashboard asset/API changes do not depend on AgentOS URL rewriting.
export const LLaMA_PROXY_URL = 'http://192.168.1.140:8082/dashboard';
export const LLaMA_PROXY_MODE = 'direct-iframe';

export function createIframeManager(doc, probeResult) {
  let node = null;
  let srcSet = false;
  let visible = false;

  function ensureNode() {
    if (node) return node;
    node = doc.createElement('iframe');
    node.setAttribute('data-mode', LLaMA_PROXY_MODE);
    node.setAttribute('data-probe', probeResult || '');
    node.setAttribute('title', 'llama-proxy dashboard (retained iframe)');
    node.style.width = '100%';
    node.style.height = '100%';
    node.style.border = '0';
    node.style.display = 'none';
    doc.body.appendChild(node);
    return node;
  }

  return {
    diagnostics: { mode: LLaMA_PROXY_MODE, probe: probeResult || '' },
    show() {
      const el = ensureNode();
      if (!srcSet) {
        el.src = LLaMA_PROXY_URL; // set exactly once; hide/show never reloads
        srcSet = true;
      }
      el.style.display = 'block';
      visible = true;
    },
    hide() {
      if (!node) return;
      node.style.display = 'none';
      visible = false;
    },
    isVisible() {
      return visible;
    },
    isCreated() {
      return node !== null;
    },
  };
}
