import { StrictMode } from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { App } from './App';
import { create_demo_ui_application } from './bootstrap';

describe('App runtime wiring', () => {
    it('LIVE hover는 백엔드 Binance 상태를 조회하고 영역 이탈 즉시 툴팁과 조회를 닫는다', async () => {
        const user = userEvent.setup();
        const application = create_demo_ui_application();
        const load_status = vi.fn().mockResolvedValue({
            api: 'online', market_stream: 'offline', account_stream: 'online',
        });
        render(<App applicationFactory={() => ({
            ...application,
            load_binance_connection_status: load_status,
        })} />);

        const live_badge = await screen.findByRole('button', { name: '화면 연결 상태: 연결됨' });
        expect(load_status).not.toHaveBeenCalled();
        expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();

        await user.hover(live_badge);
        const tooltip = await screen.findByRole('tooltip');
        expect(await within(tooltip).findByText('연결 안 됨')).toBeInTheDocument();
        expect(within(tooltip).getAllByText('연결됨')).toHaveLength(2);
        expect(load_status).toHaveBeenCalledTimes(1);

        await user.unhover(live_badge);
        expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
        expect(load_status.mock.calls[0]?.[0].aborted).toBe(true);
    });

    it('초기 REGIME 선택부터 자동매매 시작, route, CSV와 중지 흐름을 연결한다', async () => {
        const user = userEvent.setup();

        render(
            <StrictMode>
                <App applicationFactory={create_demo_ui_application} />
            </StrictMode>,
        );

        expect(await screen.findByRole('button', { name: '화면 연결 상태: 연결됨' })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: '자동매매 실행' })).toBeEnabled();
        expect(screen.getByRole('button', { name: '매매 중지' })).toBeDisabled();
        expect(await screen.findByRole('button', { name: 'type0 횡보 적용 요청' })).toHaveAttribute(
            'aria-pressed',
            'false',
        );

        await user.click(screen.getByRole('button', { name: '자동매매 실행' }));
        const regime_notice = await screen.findByRole('dialog', {
            name: 'REGIME type을 먼저 선택해주세요',
        });
        await user.click(within(regime_notice).getByRole('button', { name: 'REGIME 선택' }));
        expect(screen.getByRole('region', { name: 'REGIME 판단 패널' })).toHaveAttribute(
            'data-highlighted',
            'true',
        );

        // 변경된 REGIME 선택 확인 문구를 통해 기존 후보 적용 흐름을 검증한다.
        await user.click(screen.getByRole('button', { name: 'type0 횡보 적용 요청' }));
        const regime_dialog = await screen.findByRole('dialog', {
            name: 'REGIME type을 선택할까요?',
        });
        await user.click(within(regime_dialog).getByRole('button', { name: '확인' }));  // 후보 REGIME 선택을 확정한다.
        await waitFor(() => {
            expect(screen.getByRole('button', { name: 'type0 횡보 적용 요청' })).toHaveAttribute(
                'aria-pressed',
                'true',
            );
        });

        await user.click(screen.getByRole('button', { name: '자동매매 실행' }));
        const start_dialog = await screen.findByRole('dialog', {
            name: '자동매매를 시작할까요?',
        });
        await user.click(within(start_dialog).getByRole('button', { name: '거래 시작' }));
        await waitFor(() => {
            expect(screen.getByRole('button', { name: /자동매매 실행 중/ })).toBeDisabled();
            expect(screen.getByRole('button', { name: '매매 중지' })).toBeEnabled();
        });

        await user.click(screen.getByRole('button', { name: '전체 보기' }));
        expect(await screen.findByRole('heading', { name: '거래 내역 상세' })).toBeInTheDocument();

        await user.click(screen.getByRole('button', { name: '매도' }));
        await waitFor(() => {
            expect(screen.queryByText('26/06/22 - 10:42:18')).not.toBeInTheDocument();
            expect(screen.getByText('26/06/22 - 09:54:06')).toBeInTheDocument();
        });

        await user.click(screen.getByRole('button', { name: 'CSV 내보내기' }));
        let csv_dialog = await screen.findByRole('dialog', { name: 'CSV 내보내기' });
        await user.click(within(csv_dialog).getByRole('button', { name: '위치 선택' }));
        expect(await within(csv_dialog).findByText('/Users/demo/Exports')).toBeInTheDocument();
        await user.click(within(csv_dialog).getByRole('button', { name: '내보내기' }));

        const complete_dialog = await screen.findByRole('dialog', { name: 'CSV 내보내기 완료' });
        expect(within(complete_dialog).getByText(/ETH_trade_history_260629\.csv/)).toBeInTheDocument();
        await user.click(within(complete_dialog).getByRole('button', { name: '확인' }));

        await user.click(screen.getByRole('button', { name: /돌아가기/ }));
        await user.click(screen.getByRole('button', { name: '매매 중지' }));
        const stop_dialog = await screen.findByRole('dialog', { name: '매매를 중지할까요?' });
        await user.click(within(stop_dialog).getByRole('button', { name: '매매 중지' }));

        await waitFor(() => {
            expect(screen.getByRole('button', { name: '자동매매 실행' })).toBeEnabled();
        });
    });
});
