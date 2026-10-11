import { useState } from 'react';
import { Alert, Button, Card, Empty, Input, Message, Radio, Tag, Typography } from '@arco-design/web-react';
import Header from '@/components/Header';
import {
  knowledgeBaseApi,
  KnowledgeHit,
  MemoryVectorHit,
} from '@/app/knowledgeBaseApi';
import styles from './index.module.less';

type SearchSource = 'rag' | 'memory';

interface SearchHit {
  id: string;
  title: string;
  content: string;
  score: number | null;
  source: string;
  metadata: Record<string, unknown>;
}

function parseMetadata(value: string): Record<string, unknown> | undefined {
  if (!value.trim()) return undefined;
  const parsed: unknown = JSON.parse(value);
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
    throw new Error('Metadata 必须是 JSON 对象。');
  }
  return parsed as Record<string, unknown>;
}

export default function RetrievalTestPage(): JSX.Element {
  const [query, setQuery] = useState('');
  const [topK, setTopK] = useState('5');
  const [filterText, setFilterText] = useState('');
  const [source, setSource] = useState<SearchSource>('rag');
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<SearchHit[] | null>(null);

  const handleSearch = async () => {
    if (!query.trim()) {
      Message.warning('请输入检索问题。');
      return;
    }
    try {
      const count = Number(topK);
      if (!Number.isInteger(count) || count < 1 || count > 20) {
        throw new Error('Top K 必须是 1 到 20 的整数。');
      }
      const metadata = source === 'rag' ? parseMetadata(filterText) : undefined;
      setSearching(true);
      setResults(null);
      if (source === 'rag') {
        const response = await knowledgeBaseApi.search(query.trim(), count, metadata);
        setResults(response.results.map((hit: KnowledgeHit) => ({
          id: hit.chunk_id,
          title: hit.title || '知识库切片',
          content: hit.content,
          score: hit.score,
          source: typeof hit.metadata.path === 'string' ? hit.metadata.path : hit.document_id,
          metadata: hit.metadata || {},
        })));
      } else {
        const response = await knowledgeBaseApi.searchMemories(query.trim(), count);
        setResults(response.results.map((hit: MemoryVectorHit) => {
          const scope = hit.metadata.scope === 'project'
            ? `项目：${String(hit.metadata.project_name || hit.metadata.subject_project_id || '未命名')}`
            : '全局记忆';
          const role = typeof hit.metadata.memory_role === 'string'
            ? hit.metadata.memory_role
            : '长期记忆';
          return {
            id: hit.id,
            title: `${scope} · ${role}`,
            content: hit.content,
            score: hit.score,
            source: `记忆 ID：${hit.id}`,
            metadata: { ...hit.metadata, tags: hit.tags },
          };
        }));
      }
    } catch (error) {
      Message.error(error instanceof Error ? error.message : '检索失败。');
    } finally {
      setSearching(false);
    }
  };

  return (
    <div className={styles.page}>
      <Header />
      <main className={styles.content}>
        <div className={styles.heading}>
          <Typography.Title heading={3}>向量检索测试</Typography.Title>
          <Typography.Text type="secondary">
            查询 Gateway 中的 RAG 切片或当前登录用户的长期记忆，不包含文档管理功能。
          </Typography.Text>
        </div>
        <Card className={styles.card} title="检索测试" bordered={false}>
          <Radio.Group
            className={styles.searchSources}
            type="button"
            value={source}
            disabled={searching}
            onChange={(value) => {
              setSource(value as SearchSource);
              setResults(null);
            }}
            aria-label="向量检索来源"
          >
            <Radio value="rag">RAG 知识库</Radio>
            <Radio value="memory">长期记忆</Radio>
          </Radio.Group>
          {source === 'memory' && (
            <Alert
              className={styles.hint}
              type="info"
              content="仅检索当前登录用户的长期记忆，范围包含全局与项目记忆。"
            />
          )}
          <Input.TextArea
            value={query}
            onChange={setQuery}
            placeholder={source === 'rag' ? '输入一个问题，查询相关知识库切片' : '输入一个问题，查询相关长期记忆'}
            autoSize={{ minRows: 3, maxRows: 6 }}
            disabled={searching}
            onPressEnter={(event) => {
              if (!event.shiftKey) {
                event.preventDefault();
                void handleSearch();
              }
            }}
          />
          <div className={styles.controls}>
            <Input
              value={topK}
              onChange={setTopK}
              placeholder="Top K（1–20）"
              disabled={searching}
              aria-label="Top K"
            />
            <Button type="primary" loading={searching} onClick={() => void handleSearch()}>
              检索
            </Button>
          </div>
          {source === 'rag' ? (
            <Input.TextArea
              className={styles.filter}
              value={filterText}
              onChange={setFilterText}
              placeholder='Metadata 精确过滤（可选），例如：{"category":"售后"}'
              autoSize={{ minRows: 2, maxRows: 4 }}
              disabled={searching}
              aria-label="RAG metadata 过滤条件"
            />
          ) : null}
          {results !== null && (
            <div className={styles.results}>
              {results.length === 0 ? (
                <Empty description={source === 'rag' ? '未找到相关片段' : '未找到相关长期记忆'} />
              ) : results.map((hit, index) => (
                <Card className={styles.hit} key={`${hit.id}-${index}`} bordered>
                  <div className={styles.hitHeading}>
                    <Typography.Text bold>{hit.title}</Typography.Text>
                    <Tag color="arcoblue">
                      Score {typeof hit.score === 'number' ? hit.score.toFixed(3) : '—'}
                    </Tag>
                  </div>
                  <Typography.Paragraph className={styles.hitContent}>{hit.content}</Typography.Paragraph>
                  <Typography.Text type="secondary">来源：{hit.source}</Typography.Text>
                  {Object.keys(hit.metadata).length > 0 && (
                    <pre className={styles.metadata}>{JSON.stringify(hit.metadata, null, 2)}</pre>
                  )}
                </Card>
              ))}
            </div>
          )}
        </Card>
      </main>
    </div>
  );
}
