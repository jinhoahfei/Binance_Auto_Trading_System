// 서버 소유 snapshot을 위한 QueryClient를 애플리케이션 생명주기에 맞춰 제공한다.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { useState } from 'react';

export interface AppProvidersProps {
  children: ReactNode;
}


/**
 * 함수 이름: AppProviders()
 * 기능: 서버 소유 snapshot을 위한 QueryClient를 애플리케이션 생명주기에 맞춰 제공한다.
 * 인자: props -> provider 내부에 표시할 React 내용
 * 반환값: 공통 provider가 적용된 React 요소
 * 작성 날짜: 2026/08/12
 */
export function AppProviders({ children }: AppProvidersProps) {
  const [query_client] = useState(() => new QueryClient({
    defaultOptions: {
      queries: {
        refetchOnWindowFocus: false,
        retry: 1,
        staleTime: 5_000,
      },
    },
  }));

  return <QueryClientProvider client={query_client}>{children}</QueryClientProvider>;
}
