export interface Agent {
  id: string;
  name: string;
  version_id: string;
  number: number;
  config: {
    system_prompt: string;
    closed_loop?: boolean;
    tools: string[];
    token_budget: number;
    max_steps: number;
    max_tool_calls: number;
    max_output_tokens: number;
    timeout_seconds: number;
    provider: string;
    model: string;
  };
}
