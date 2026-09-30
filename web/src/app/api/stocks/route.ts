import { json } from "@/lib/http";
import { STOCKS } from "@/lib/stocks";

export const dynamic = "force-dynamic";

export function GET() {
  return json(STOCKS);
}
