import React, {useEffect} from 'react';

/** Drop stale OpenAPI explorer server (old docs used :5175 / viknow2). */
export default function Root({children}: {children: React.ReactNode}): React.ReactNode {
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem('server');
      if (!raw) return;
      if (raw.includes(':5175') || raw.includes('5175')) {
        sessionStorage.removeItem('server');
      }
    } catch {
      /* ignore */
    }
  }, []);
  return <>{children}</>;
}
