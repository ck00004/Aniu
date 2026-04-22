export function parseSseChunk<T = Record<string, unknown>>(
  chunk: string,
  onParseError?: (error: unknown, payload: string) => void,
): T | null {
  const dataLines: string[] = []
  for (const raw of chunk.split('\n')) {
    const line = raw.trimEnd()
    if (!line || line.startsWith(':')) continue
    if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trimStart())
    }
  }
  if (!dataLines.length) return null
  const payload = dataLines.join('\n')
  try {
    return JSON.parse(payload) as T
  } catch (error) {
    onParseError?.(error, payload)
    return null
  }
}
