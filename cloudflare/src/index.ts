import { DurableObject } from "cloudflare:workers";

interface Env {
  SCOUT_PAY: DurableObjectNamespace<ScoutPayContainer>;
  CF_VERSION: WorkerVersionMetadata;
  BRAINTREE_ENVIRONMENT: string;
  BRAINTREE_MERCHANT_ID: string;
  BRAINTREE_PUBLIC_KEY?: string;   // secret
  BRAINTREE_PRIVATE_KEY?: string;  // secret
  APPROVAL_THRESHOLD: string;
  LLM_PROVIDER: string;
  BEDROCK_MODEL_ID?: string;
  AWS_REGION?: string;
  AWS_ACCESS_KEY_ID?: string;      // secret (only if LLM_PROVIDER=bedrock)
  AWS_SECRET_ACCESS_KEY?: string;  // secret
  APPROVER_KEY?: string;           // secret: locks approvals, refunds, full audit log
}

const PASS = ["BRAINTREE_ENVIRONMENT", "BRAINTREE_MERCHANT_ID", "BRAINTREE_PUBLIC_KEY", "BRAINTREE_PRIVATE_KEY",
  "APPROVAL_THRESHOLD", "LLM_PROVIDER", "BEDROCK_MODEL_ID", "AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "APPROVER_KEY"] as const;

const PORT = 8080;
const IDLE_MS = 15 * 60 * 1000;

// "durable_object" scheduling policy: this DO starts the container itself with the image wrangler built.
export class ScoutPayContainer extends DurableObject<Env> {
  // Called over RPC: with a container-backed DO, stub.fetch() goes straight to the container
  // and skips this code, so the Worker calls proxy() instead.
  async proxy(request: Request): Promise<Response> {
    const c = this.ctx.container!;
    if (!c.running) {
      const env: Record<string, string> = {};
      for (const k of PASS) { const v = this.env[k]; if (v) env[k] = v; }
      c.start({ image: c.images.base, instance: "basic", enableInternet: true, env });
      c.monitor().catch((e) => console.error("container exited", e));
      await c.setInactivityTimeout(IDLE_MS);
      await this.waitReady();
    }
    // The container port only speaks plain HTTP (the hop is already private).
    const url = new URL(request.url);
    url.protocol = "http:";
    try {
      return await c.getTcpPort(PORT).fetch(new Request(url, request));
    } catch (e) {
      console.error("proxy failed", url.href, e);
      return new Response(`container proxy error: ${e}`, { status: 502 });
    }
  }

  // Poll until uvicorn answers (cold start), up to ~60s.
  private async waitReady() {
    const port = this.ctx.container!.getTcpPort(PORT);
    for (let i = 0; i < 120; i++) {
      try { await port.fetch("http://container/healthz"); return; }
      catch { await new Promise((r) => setTimeout(r, 500)); }
    }
    throw new Error("container did not become ready");
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // Single instance per deployment: a running DO keeps its old code and container across deploys,
    // so each version gets a fresh one (the old one sleeps after IDLE_MS). Approvals are in-memory
    // until D1 persistence lands, so they reset on deploy.
    return env.SCOUT_PAY.get(env.SCOUT_PAY.idFromName(env.CF_VERSION.id)).proxy(request);
  },
};
