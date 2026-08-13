import type { Preview } from '@storybook/react-vite';

import '../src/shared/styles/global.css';

const preview: Preview = {
  parameters: {
    a11y: {
      test: 'error',
    },
    backgrounds: {
      default: 'canvas',
      values: [{ name: 'canvas', value: '#0b0e11' }],
    },
    layout: 'fullscreen',
  },
};

export default preview;
