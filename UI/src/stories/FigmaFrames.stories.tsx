// FigmaFrames의 표시 상태를 Storybook 시나리오로 제공한다.

import type { Meta, StoryObj } from '@storybook/react-vite';

import { FigmaFrameHarness } from './figma-frames/FigmaFrameHarness';
import type { FigmaFrameKey } from './figma-frames/figmaFrameFixtures';

const meta = {
  title: 'Figma Frames/Binance Auto Trader',
  component: FigmaFrameHarness,
  parameters: {
    layout: 'fullscreen',
    chromatic: {
      viewports: [1440],
    },
    viewport: {
      defaultViewport: 'responsive',
    },
  },
  args: {
    frame: 'realtime-indicator',
  },
} satisfies Meta<typeof FigmaFrameHarness>;

export default meta;

type Story = StoryObj<typeof meta>;


/**
 * 함수 이름: create_figma_frame_story()
 * 기능: 공통 FigmaFrameHarness에 프레임 키 하나만 주입하는 Story 정의를 만든다.
 * 인자: frame -> visual regression 기준 프레임 키
 * 반환값: Storybook에서 렌더링할 단일 Story 정의
 * 작성 날짜: 2026/08/12
 */
function create_figma_frame_story(frame: FigmaFrameKey): Story {
  return {
    args: { frame },
  };
}

export const Frame01RealtimeIndicator = create_figma_frame_story('realtime-indicator');
export const Frame02RecentOrders = create_figma_frame_story('recent-orders');
export const Frame03IndicatorSettings = create_figma_frame_story('indicator-settings');
export const Frame04TradeHistory = create_figma_frame_story('trade-history');
export const Frame05StartConfirmation = create_figma_frame_story('start-confirmation');
export const Frame06StopWithPosition = create_figma_frame_story('stop-with-position');
export const Frame07StopWithoutPosition = create_figma_frame_story('stop-without-position');
export const Frame08CsvEndCalendar = create_figma_frame_story('csv-end-calendar');
export const Frame09CsvStartCalendar = create_figma_frame_story('csv-start-calendar');
export const Frame10CsvDialog = create_figma_frame_story('csv-dialog');
export const Frame11RegimeConfirmation = create_figma_frame_story('regime-confirmation');
export const Frame12RegimeRequired = create_figma_frame_story('regime-required');
export const Frame13RegimeHighlight = create_figma_frame_story('regime-highlight');
export const Frame14HistoryEmpty = create_figma_frame_story('history-empty');
export const Frame15StartLoading = create_figma_frame_story('start-loading');
export const Frame16CsvValidationError = create_figma_frame_story('csv-validation-error');
