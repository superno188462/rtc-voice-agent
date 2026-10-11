/** Types and authenticated API calls for the Gateway backed knowledge base. */
import { AIGC_PROXY_HOST } from '@/config';

export interface KnowledgeBaseStatus {
  configured: boolean;
  exists: boolean;
  collection?: string;
  model?: string;
  vector_dimensions?: number | null;
  chunk_size?: number | null;
  chunk_overlap?: number | null;
}

export interface KnowledgeDocumentInput {
  document_id?: string;
  directory?: string;
  name?: string;
  source_type?: 'file' | 'text';
  file_name?: string;
  title: string;
  content: string;
  metadata: Record<string, unknown>;
}

export interface KnowledgeDocumentResult {
  document_id: string;
  path?: string | null;
  directory?: string;
  name?: string;
  source_type?: 'file' | 'text';
  title?: string;
  file_name?: string | null;
  sync_status?: string;
  chunk_count?: number;
  result?: {
    external_id?: string;
    chunks?: number;
    vectors?: number;
    action?: 'created' | 'updated' | 'unchanged';
  };
}

export interface KnowledgeHit {
  chunk_id: string;
  document_id: string;
  title: string;
  content: string;
  score: number | null;
  metadata: Record<string, unknown>;
}

export interface MemoryVectorHit {
  id: string;
  content: string;
  score: number | null;
  tags: string[];
  metadata: Record<string, unknown>;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${AIGC_PROXY_HOST}${path}`, {
      ...init,
      credentials: 'include',
      headers: {
        'content-type': 'application/json',
        ...init?.headers,
      },
    });
  } catch {
    throw new Error('无法连接项目后端，请确认 Python 服务已启动。');
  }

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload?.detail;
    const message =
      typeof detail?.message === 'string' ? detail.message : '检索或知识库操作失败，请稍后重试。';
    throw new Error(message);
  }
  return payload as T;
}

export const knowledgeBaseApi = {
  getStatus: () => request<KnowledgeBaseStatus>('/api/knowledge-base'),
  initialize: (description: string, embeddingModel: string, chunkSize: number) =>
    request<Record<string, unknown>>('/api/knowledge-base/initialize', {
      method: 'POST',
      body: JSON.stringify({ description, embedding_model: embeddingModel, chunk_size: chunkSize }),
    }),
  listDocuments: () =>
    request<{ documents: KnowledgeDocumentResult[] }>('/api/knowledge-base/documents'),
  upsertDocuments: (documents: KnowledgeDocumentInput[]) =>
    request<{ documents: KnowledgeDocumentResult[] }>('/api/knowledge-base/documents/batch', {
      method: 'POST',
      body: JSON.stringify({ documents }),
    }),
  deleteDocument: (documentId: string) =>
    request<Record<string, unknown>>(
      `/api/knowledge-base/documents?${new URLSearchParams({ document_id: documentId })}`,
      { method: 'DELETE' }
    ),
  search: (query: string, topK: number, metadata?: Record<string, unknown>) =>
    request<{ results: KnowledgeHit[] }>('/api/knowledge-base/search', {
      method: 'POST',
      body: JSON.stringify({ query, top_k: topK, metadata }),
    }),
  searchMemories: (query: string, topK: number) =>
    request<{ results: MemoryVectorHit[] }>('/api/agent/memories/search', {
      method: 'POST',
      body: JSON.stringify({ query, top_k: topK }),
    }),
};
