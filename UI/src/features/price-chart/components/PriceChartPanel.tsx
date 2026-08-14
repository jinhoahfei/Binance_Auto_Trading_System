import { useEffect } from 'react';

import { ChartCanvas } from './ChartCanvas';
import { ChartToolbar } from './ChartToolbar';
import { IndicatorSettingsPopover } from './IndicatorSettingsPopover';
import type { PriceChartPanelProps } from '../types';
import styles from './PriceChartPanel.module.css';

/**
 * 함수 이름: PriceChartPanel()
 * 기능: ETH 차트 제목, 주기 도구 모음과 Lightweight Charts 기반 캔들 차트를 표시한다.
 * 인자: props -> 가격 차트 ViewModel과 사용자 intent 처리 함수
 * 반환값: ETH 가격 차트 패널 React 요소
 * 작성 날짜: 2026/08/12
 */
export function PriceChartPanel({
    activeState,
    bollingerLower,
    bollingerUpper,
    candles,
    contextMenuPosition = null,
    drawingActive = false,
    drawings = [],
    ema,
    indicatorSettings,
    indicatorSettingsOpen = false,
    interval,
    isFullscreen = false,
    lineContextMenuOpen = false,
    onIntent,
    selectedLineId = null,
    timestampLabel,
}: PriceChartPanelProps) {
    useEffect(() => {
        if (!indicatorSettingsOpen) {
            return undefined;
        }

        const handle_outside_pointer_down = (event: PointerEvent) => {
            if (!(event.target instanceof Element)
                || event.target.closest('[data-indicator-settings-popover]') !== null
                || event.target.closest('[data-indicator-settings-trigger]') !== null) {
                return;
            }

            onIntent?.({ type: 'INDICATOR_SETTINGS_CLOSED' });
        };

        document.addEventListener('pointerdown', handle_outside_pointer_down);

        return () => document.removeEventListener('pointerdown', handle_outside_pointer_down);
    }, [indicatorSettingsOpen, onIntent]);

    return (
        <section
            aria-labelledby="price-chart-title"
            className={`${styles.panel} ${isFullscreen ? styles.fullscreenPanel : ''}`}
        >
            <div className={styles.header}>
                <div className={styles.heading}>
                    <h2 id="price-chart-title">ETH 가격 차트</h2>
                    <p>{timestampLabel}</p>
                </div>
                <ChartToolbar
                    activeState={activeState}
                    indicatorSettingsOpen={indicatorSettingsOpen}
                    interval={interval}
                    onIntent={onIntent}
                />
            </div>
            <ChartCanvas
                bollingerLower={bollingerLower}
                bollingerUpper={bollingerUpper}
                candles={candles}
                contextMenuPosition={contextMenuPosition}
                drawingActive={drawingActive}
                drawings={drawings}
                ema={ema}
                indicatorSettings={indicatorSettings}
                isFullscreen={isFullscreen}
                lineContextMenuOpen={lineContextMenuOpen}
                onIntent={onIntent}
                selectedLineId={selectedLineId}
            />
            {indicatorSettingsOpen && indicatorSettings !== undefined ? (
                <IndicatorSettingsPopover onIntent={onIntent} settings={indicatorSettings} />
            ) : null}
        </section>
    );
}
