export interface Skill {
  id: string;
  name: string;
  description: string;
  version_id: string;
  number: number;
  allowed_tools: string[];
  content_hash: string;
  enabled: boolean;
  can_manage: boolean;
  instructions?: string;
}
