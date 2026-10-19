import type { Source } from "../rag/api";
export interface Conversation {
  id: string;
  title: string;
}
export interface Message {
  id: string;
  role: string;
  content: string;
  sources?: Source[];
}
