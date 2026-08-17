export const SKILL_PROMPT_MODES = Object.freeze(['prune', 'invisible']);
export const INHERIT_SKILL_PROMPT_MODE = 'inherit';

function normalizedSkillNames(value) {
  if (!Array.isArray(value)) return null;
  const names = [];
  for (const name of value) {
    if (typeof name !== 'string' || !name || name.trim() !== name) return null;
    names.push(name);
  }
  return [...new Set(names)].sort();
}

export function normalizeSkillPromptMode(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  if (Object.keys(value).some((key) => !SKILL_PROMPT_MODES.includes(key))) return null;
  const prune = normalizedSkillNames(value.prune ?? []);
  const invisible = normalizedSkillNames(value.invisible ?? []);
  if (!prune || !invisible) return null;
  if (prune.some((name) => invisible.includes(name))) return null;
  return { prune, invisible };
}

export function isSkillPromptMode(value) {
  return normalizeSkillPromptMode(value) !== null;
}

export function emptySkillPromptMode() {
  return { prune: [], invisible: [] };
}

export function profileSkillPromptMode(config) {
  const value = config?.skills?.mode ?? {};
  return normalizeSkillPromptMode(value);
}

/** Match the gateway's typed-first group_topics precedence. */
export function topicExtraPath(config) {
  const typed = config?.platforms?.telegram?.extra;
  if (typed && typeof typed === 'object' && 'group_topics' in typed) {
    return ['platforms', 'telegram', 'extra'];
  }
  return ['telegram', 'extra'];
}

function atPath(root, path) {
  return path.reduce(
    (node, key) => (node && typeof node === 'object' ? node[key] : undefined),
    root,
  );
}

function nestedBody(path, key, value) {
  const body = {};
  let cursor = body;
  for (const segment of path) {
    cursor[segment] = {};
    cursor = cursor[segment];
  }
  cursor[key] = value;
  return body;
}

export function configuredSkillModeTopics(config) {
  const path = topicExtraPath(config);
  const groups = atPath(config, path)?.group_topics;
  const topics = [];
  for (const group of Array.isArray(groups) ? groups : []) {
    for (const topic of Array.isArray(group?.topics) ? group.topics : []) {
      topics.push({
        chatId: String(group?.chat_id ?? ''),
        chatName: group?.name || group?.title || '',
        threadId: String(topic?.thread_id ?? ''),
        name: topic?.name || `thread ${topic?.thread_id ?? ''}`,
        mode: topic?.skills_mode === undefined
          ? INHERIT_SKILL_PROMPT_MODE
          : normalizeSkillPromptMode(topic.skills_mode),
      });
    }
  }
  return topics;
}

export function profileSkillModePatch(mode) {
  const normalized = normalizeSkillPromptMode(mode);
  if (!normalized) throw new Error('Invalid skills mode policy');
  return { skills: { mode: normalized } };
}

/**
 * Rewrite the one list entry the operator selected while preserving every
 * sibling and every unrelated field byte-for-byte at the object level.
 */
export function topicSkillModePatch(config, chatId, threadId, mode) {
  const normalized = mode === INHERIT_SKILL_PROMPT_MODE
    ? INHERIT_SKILL_PROMPT_MODE
    : normalizeSkillPromptMode(mode);
  if (!normalized) throw new Error('Invalid topic skills mode policy');
  const path = topicExtraPath(config);
  const groups = atPath(config, path)?.group_topics;
  let matched = false;
  const next = (Array.isArray(groups) ? groups : []).map((group) => {
    if (String(group?.chat_id ?? '') !== String(chatId)) return group;
    return {
      ...group,
      topics: (Array.isArray(group?.topics) ? group.topics : []).map((topic) => {
        if (String(topic?.thread_id ?? '') !== String(threadId)) return topic;
        matched = true;
        const updated = { ...topic };
        if (normalized === INHERIT_SKILL_PROMPT_MODE) delete updated.skills_mode;
        else updated.skills_mode = normalized;
        return updated;
      }),
    };
  });
  if (!matched) throw new Error('Configured Telegram topic was not found');
  return nestedBody(path, 'group_topics', next);
}
