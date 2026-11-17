import type { Source } from "../rag/api";
export interface Conversation {
  id: string;
  title: string;
}
export interface Message {
  id: string;
  sequence?: number;
  run_id?: string | null;
  role: string;
  content: string;
  sources?: Source[];
  attachments?: Attachment[];
  uploaded_files?: { filename: string; size: number; truncated: boolean }[];
}
export interface Attachment {
  filename: string;
  size: number;
  available: boolean;
  download_url: string | null;
}
