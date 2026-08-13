import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './app/App';
import { AppProviders } from './app/providers/AppProviders';
import './shared/styles/global.css';

const root_element = document.getElementById('root');

if (root_element === null) {
  throw new Error('React 애플리케이션을 연결할 root 요소가 없습니다.');
}

createRoot(root_element).render(
  <StrictMode>
    <AppProviders>
      <App />
    </AppProviders>
  </StrictMode>,
);
