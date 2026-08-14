import { fireEvent, render, screen } from '@testing-library/react';
import { vi } from 'vitest';
import { CHART_DRAWING_FIXTURE } from '../../../shared/testing';
import { PriceChartPanel } from './PriceChartPanel';

const CHART_DATA = {
    activeState: 'Basic Iterative',
    timestampLabel: '2026.06.22 · 10:59 KST',
    interval: '1m' as const,
    candles: [{ open: 100, close: 110, high: 115, low: 95 }],
    ema: [{ value: 105 }],
    bollingerUpper: [{ value: 114 }],
    bollingerLower: [{ value: 96 }],
};

describe('PriceChartPanel', () => {
    it('DC1-03, CR-18: 주기·드로잉·전체화면 intent를 전달한다', () => {
        const handle_intent = vi.fn();

        render(<PriceChartPanel {...CHART_DATA} onIntent={handle_intent} />);

        fireEvent.click(screen.getByRole('button', { name: '4시간' }));
        fireEvent.click(screen.getByRole('button', { name: '차트 드로잉 모드' }));
        fireEvent.click(screen.getByRole('button', { name: '차트 전체화면' }));

        expect(handle_intent).toHaveBeenNthCalledWith(1, {
            type: 'CHART_INTERVAL_REQUESTED',
            interval: '4h',
        });
        expect(handle_intent).toHaveBeenNthCalledWith(2, { type: 'DRAWING_MODE_REQUESTED' });
        expect(handle_intent).toHaveBeenNthCalledWith(3, { type: 'CHART_FULLSCREEN_REQUESTED' });
    });

    it('CR-05: controlled 지표 팝오버에서 표시 변경 intent를 전달한다', () => {
        const handle_intent = vi.fn();

        render(
            <PriceChartPanel
                {...CHART_DATA}
                indicatorSettings={{ ema9: true, bollingerBand: true, volume: false }}
                indicatorSettingsOpen
                onIntent={handle_intent}
            />,
        );

        fireEvent.click(screen.getByRole('button', { name: '거래량(Volume) 표시하기' }));
        expect(handle_intent).toHaveBeenCalledWith({
            type: 'INDICATOR_VISIBILITY_REQUESTED',
            indicator: 'volume',
            visible: true,
        });
    });

    it('지표 팝오버 바깥을 누르면 닫기 intent를 전달한다', () => {
        const handle_intent = vi.fn();

        render(
            <PriceChartPanel
                {...CHART_DATA}
                indicatorSettings={{ ema9: true, bollingerBand: true, volume: false }}
                indicatorSettingsOpen
                onIntent={handle_intent}
            />,
        );

        expect(screen.getByRole('button', { name: '지표 설정' })).toHaveAttribute(
            'aria-expanded',
            'true',
        );
        fireEvent.pointerDown(document.body);

        expect(handle_intent).toHaveBeenCalledWith({ type: 'INDICATOR_SETTINGS_CLOSED' });
    });

    it('IP1~IP3: controlled 지표 표시 상태를 차트 렌더링에 반영한다', () => {
        const { container } = render(
            <PriceChartPanel
                {...CHART_DATA}
                indicatorSettings={{ ema9: false, bollingerBand: false, volume: true }}
            />,
        );

        expect(container.querySelector('[data-indicator="ema9"]')).not.toBeInTheDocument();
        expect(container.querySelector('[data-indicator="bollinger-band"]')).not.toBeInTheDocument();
        expect(container.querySelector('[data-indicator="volume"]')).toBeInTheDocument();
    });

    it('CR-18, ER-17: 전체화면 상태에서 종료 control을 표시한다', () => {
        render(<PriceChartPanel {...CHART_DATA} isFullscreen />);

        expect(screen.getByRole('button', { name: '차트 전체화면 종료' })).toBeInTheDocument();
    });

    it('DC6-02~06: 저장된 선의 hover, context menu와 삭제 intent를 전달한다', () => {
        const handle_intent = vi.fn();

        render(
            <PriceChartPanel
                {...CHART_DATA}
                contextMenuPosition={{ x: 20, y: 20 }}
                drawings={[CHART_DRAWING_FIXTURE]}
                lineContextMenuOpen
                onIntent={handle_intent}
                selectedLineId={CHART_DRAWING_FIXTURE.id}
            />,
        );

        const drawing_line = screen.getByRole('button', {
            name: `저장된 차트 선 ${CHART_DRAWING_FIXTURE.id}`,
        });
        fireEvent.mouseEnter(drawing_line);
        fireEvent.contextMenu(drawing_line);
        fireEvent.click(screen.getByRole('menuitem', { name: '선 삭제' }));

        expect(handle_intent).toHaveBeenNthCalledWith(1, {
            type: 'DRAWING_LINE_HOVER_ENTERED',
            lineId: CHART_DRAWING_FIXTURE.id,
        });
        expect(handle_intent).toHaveBeenNthCalledWith(2, {
            type: 'DRAWING_LINE_CONTEXT_MENU_REQUESTED',
            x: 0,
            y: 0,
        });
        expect(handle_intent).toHaveBeenNthCalledWith(3, {
            type: 'DRAWING_LINE_DELETE_REQUESTED',
        });
    });

    it('저장된 선의 context menu 바깥을 누르면 닫기 intent를 전달한다', () => {
        const handle_intent = vi.fn();

        render(
            <PriceChartPanel
                {...CHART_DATA}
                contextMenuPosition={{ x: 20, y: 20 }}
                drawings={[CHART_DRAWING_FIXTURE]}
                lineContextMenuOpen
                onIntent={handle_intent}
                selectedLineId={CHART_DRAWING_FIXTURE.id}
            />,
        );

        fireEvent.pointerDown(document.body);
        expect(handle_intent).toHaveBeenCalledWith({
            type: 'DRAWING_LINE_CONTEXT_MENU_CLOSED',
        });
    });

    it('DC5-03~05: 두 포인터 입력을 drawing 시작과 완료 intent로 전달한다', () => {
        const handle_intent = vi.fn();
        const { container } = render(
            <PriceChartPanel {...CHART_DATA} drawingActive onIntent={handle_intent} />,
        );
        const chart = container.querySelector('svg');

        expect(chart).not.toBeNull();
        fireEvent.pointerDown(chart!, { clientX: 10, clientY: 20 });
        fireEvent.pointerMove(chart!, { clientX: 70, clientY: 80 });
        fireEvent.pointerDown(chart!, { clientX: 70, clientY: 80 });

        expect(handle_intent).toHaveBeenNthCalledWith(1, { type: 'DRAWING_STARTED' });
        expect(handle_intent.mock.calls[1]?.[0]).toMatchObject({ type: 'DRAWING_FINISHED' });
    });
});
