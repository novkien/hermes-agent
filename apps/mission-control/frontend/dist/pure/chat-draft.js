/** Runtime identity for a not-yet-created chat. */
export function resolveDraftRuntimeProfile(activeProfile, selectedProfile = undefined) {
  const value = selectedProfile === undefined ? activeProfile : selectedProfile;
  const normalized = String(value || '').trim();
  return !normalized || normalized === 'default' ? null : normalized;
}

export function draftSessionProfile(runtimeProfile) {
  return resolveDraftRuntimeProfile(null, runtimeProfile) || 'default';
}

export function buildDraftSessionRequest({
  documentProfile,
  runtimeProfile,
  model,
  provider,
  modelAllowed = false,
} = {}) {
  const body = { profile: documentProfile || 'default' };
  const runtime = resolveDraftRuntimeProfile(null, runtimeProfile);
  if (runtime) {
    body.profile_name = runtime;
  } else if (modelAllowed && model) {
    body.model = model;
    if (provider) body.provider = provider;
  }
  return body;
}
