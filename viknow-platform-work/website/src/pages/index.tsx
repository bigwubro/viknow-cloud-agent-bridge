import React from 'react';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import styles from './index.module.css';

function Card({
  to,
  title,
  children,
}: {
  to: string;
  title: string;
  children: React.ReactNode;
}): React.ReactNode {
  return (
    <Link to={to} className={styles.card}>
      <h3>
        <span>{title}</span>
        <span className={styles.cardArrow} aria-hidden>↗</span>
      </h3>
      <p>{children}</p>
    </Link>
  );
}

export default function Home(): React.ReactNode {
  const {siteConfig} = useDocusaurusContext();
  return (
    <Layout title="开发者中心" description="ViKnow 3.0 API 与指南">
      <main className={styles.hero}>
        <div className={styles.glow} aria-hidden />
        <div className={styles.inner}>
          <p className={styles.eyebrow}>ViKnow 3.0</p>
          <h1 className={styles.title}>{siteConfig.title}</h1>
          <p className={styles.subtitle}>{siteConfig.tagline}</p>
          <div className={styles.actions}>
            <Link className={styles.primary} to="/guide">
              阅读指南
            </Link>
            <Link className={styles.secondary} to="/api/viknow-3-0-api">
              API 参考
            </Link>
          </div>
          <div className={styles.cards}>
            <Card to="/api/viknow-3-0-api" title="Agent API">
              会话、轮次、SSE 事件流与多 Agent 运行接口。
            </Card>
            <Card to="/guide/hyperrag-runbook" title="Knowledge & RAG">
              知识库、索引任务、检索与 HyperRAG 运维说明。
            </Card>
            <Card to="/api/viknow-3-0-api" title="Operations">
              运维配置变更、审计与 OM 管理接口。
            </Card>
          </div>
        </div>
      </main>
    </Layout>
  );
}
