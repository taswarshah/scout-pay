import { Container, getContainer } from "@cloudflare/containers";

interface Env {
  SCOUT_PAY: DurableObjectNamespace<ScoutPayContainer>;
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
}

const PASS = ["BRAINTREE_ENVIRONMENT", "BRAINTREE_MERCHANT_ID", "BRAINTREE_PUBLIC_KEY", "BRAINTREE_PRIVATE_KEY",
  "APPROVAL_THRESHOLD", "LLM_PROVIDER", "BEDROCK_MODEL_ID", "AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"] as const;

export class ScoutPayContainer extends Container<Env> {
  defaultPort = 8080;
  sleepAfter = "15m";
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    const vars: Record<string, string> = {};
    for (const k of PASS) { const v = env[k]; if (v) vars[k] = v; }
    this.envVars = vars;
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // Single instance: approvals are in-memory until D1 persistence lands.
    return getContainer(env.SCOUT_PAY, "main").fetch(request);
  },
};
