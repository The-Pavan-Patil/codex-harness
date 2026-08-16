/**
 * Model → provider icon, by contains-check on the model name.
 *
 * Icons live in public/models/ (served from the site root). First matching
 * needle wins; unknown models render no icon.
 */

const MODEL_ICONS: [needles: string[], icon: string][] = [
  [['gpt', 'openai', 'codex', 'o3', 'o4'], '/models/openai.png'],
]

export function modelIcon(model: string | null | undefined): string | null {
  if (!model) return null
  const m = model.toLowerCase()
  for (const [needles, icon] of MODEL_ICONS) {
    if (needles.some((n) => m.includes(n))) return icon
  }
  return null
}

/** Keep provider-qualified IDs compact while preserving the full ID in titles. */
export function modelName(model: string | null | undefined): string {
  if (!model) return ''
  return model.split('/').filter(Boolean).at(-1) ?? model
}
