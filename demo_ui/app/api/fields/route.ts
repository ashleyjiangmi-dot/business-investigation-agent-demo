import { buildEntryFields } from '@/lib/entry-fields'

export async function GET() {
  try {
    return Response.json(await buildEntryFields())
  } catch (e) {
    return Response.json({ error: e instanceof Error ? e.message : String(e) }, { status: 502 })
  }
}
