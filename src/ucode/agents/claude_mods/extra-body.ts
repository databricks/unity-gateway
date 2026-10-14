/** Merge only the supplied top-level fields, preserving other request metadata. */
export function mergeExtraBody(raw: string | undefined, fields: Record<string, unknown>): string {
  const body = raw ? JSON.parse(raw) : {};
  if (body === null || Array.isArray(body) || typeof body !== 'object') {
    throw new Error('CLAUDE_CODE_EXTRA_BODY must be a JSON object.');
  }
  return JSON.stringify({ ...body, ...fields });
}
