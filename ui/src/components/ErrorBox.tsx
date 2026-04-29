import { ApiError } from "../api/client";

export function ErrorBox({ error }: { error: unknown }) {
  const text = error instanceof ApiError ? error.body : error instanceof Error ? error.message : String(error);
  return <div role="alert" className="border border-brick/30 bg-brick/10 p-3 text-sm text-brick">{text}</div>;
}
