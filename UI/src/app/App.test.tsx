import { StrictMode } from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { App } from './App';
import { create_demo_ui_application } from './bootstrap';

describe('App runtime wiring', () => {
    it('LIVE hover는 백엔드 Binance 상태를 조회하고 영역 이탈 즉시 툴팁과 조회를 닫는다', async () => {
        // Binance 진단 결과를 대역으로 주입해 외부 통신 없이 hover 동작을 검증한다.
        const user = userEvent.setup();
        const application = create_demo_ui_application();
        const load_status = vi.fn().mockResolvedValue({
            api: 'online', market_stream: 'offline', account_stream: 'online',
        });

        // 진단 조회 대역을 연결한 앱을 표시한다.
        render(<App applicationFactory={() => ({
            ...application,
            load_binance_connection_status: load_status,
        })} />);

        const live_badge = await screen.findByRole('button', { name: '화면 연결 상태: 연결됨' });

        // 툴팁을 열기 전에는 진단 요청도 설명창도 없어야 한다.
        expect(load_status).not.toHaveBeenCalled();
        expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();

        // LIVE 영역에 포인터를 올려 상세 진단을 요청한다.
        await user.hover(live_badge);
        const tooltip = await screen.findByRole('tooltip');

        // 세 연결 상태를 표시하고 진단 요청은 한 번만 실행했는지 확인한다.
        expect(await within(tooltip).findByText('연결 안 됨')).toBeInTheDocument();
        expect(within(tooltip).getAllByText('연결됨')).toHaveLength(2);
        expect(load_status).toHaveBeenCalledTimes(1);

        // 포인터를 LIVE 영역 밖으로 이동한다.
        await user.unhover(live_badge);

        // 포인터가 떠나면 설명창과 진행 중 조회를 함께 닫아야 한다.
        expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
        expect(load_status.mock.calls[0]?.[0].aborted).toBe(true);
    });

    it('초기 REGIME 선택부터 자동매매 시작, route, CSV와 중지 흐름을 연결한다', async () => {
        // StrictMode의 반복 mount에서도 초기 선택부터 중지까지 같은 runtime 흐름을 검증한다.
        const user = userEvent.setup();

        // 실제 앱 구성에 demo Controller를 주입한다.
        render(
            <StrictMode>
                <App applicationFactory={create_demo_ui_application} />
            </StrictMode>,
        );

        // 초기 화면은 연결됨·미선택 REGIME이며 중지 버튼은 비활성 상태다.
        expect(await screen.findByRole('button', { name: '화면 연결 상태: 연결됨' })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: '자동매매 실행' })).toBeEnabled();
        expect(screen.getByRole('button', { name: '매매 중지' })).toBeDisabled();
        expect(await screen.findByRole('button', { name: 'type0 횡보 적용 요청' })).toHaveAttribute(
            'aria-pressed',
            'false',
        );

        // REGIME 없이 시작하면 선택 안내를 거쳐 해당 패널로 이동한다.
        await user.click(screen.getByRole('button', { name: '자동매매 실행' }));
        const regime_notice = await screen.findByRole('dialog', {
            name: 'REGIME type을 먼저 선택해주세요',
        });
        await user.click(within(regime_notice).getByRole('button', { name: 'REGIME 선택' }));

        // 선택 안내 이후 REGIME 패널이 강조되는지 확인한다.
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

        // 확정된 REGIME으로 시작 확인을 수락한다.
        await user.click(screen.getByRole('button', { name: '자동매매 실행' }));
        const start_dialog = await screen.findByRole('dialog', {
            name: '자동매매를 시작할까요?',
        });
        await user.click(within(start_dialog).getByRole('button', { name: '거래 시작' }));

        // 시작 완료 후에는 시작 버튼이 잠기고 중지 버튼이 활성화된다.
        await waitFor(() => {
            expect(screen.getByRole('button', { name: /자동매매 실행 중/ })).toBeDisabled();
            expect(screen.getByRole('button', { name: '매매 중지' })).toBeEnabled();
        });

        // 거래 내역 상세 화면으로 전환한다.
        await user.click(screen.getByRole('button', { name: '전체 보기' }));
        expect(await screen.findByRole('heading', { name: '거래 내역 상세' })).toBeInTheDocument();  // 상세 route가 표시되는지 확인한다.

        // 매도 필터로 결과 행을 제한한다.
        await user.click(screen.getByRole('button', { name: '매도' }));

        // 매수 행은 제외하고 매도 행은 유지하는지 확인한다.
        await waitFor(() => {
            expect(screen.queryByText('26/06/22 - 10:42:18')).not.toBeInTheDocument();
            expect(screen.getByText('26/06/22 - 09:54:06')).toBeInTheDocument();
        });

        // CSV 창에서 저장 위치를 선택한다.
        await user.click(screen.getByRole('button', { name: 'CSV 내보내기' }));
        let csv_dialog = await screen.findByRole('dialog', { name: 'CSV 내보내기' });
        await user.click(within(csv_dialog).getByRole('button', { name: '위치 선택' }));
        expect(await within(csv_dialog).findByText('/Users/demo/Exports')).toBeInTheDocument();  // 선택한 저장 경로를 표시한다.

        // 선택한 위치와 옵션으로 내보내기를 요청한다.
        await user.click(within(csv_dialog).getByRole('button', { name: '내보내기' }));

        const complete_dialog = await screen.findByRole('dialog', { name: 'CSV 내보내기 완료' });
        expect(within(complete_dialog).getByText(/ETH_trade_history_260629\.csv/)).toBeInTheDocument();  // 완료 영수증의 파일명을 표시한다.

        // 완료 안내를 닫고 대시보드로 복귀해 매매 중지를 확인한다.
        await user.click(within(complete_dialog).getByRole('button', { name: '확인' }));

        await user.click(screen.getByRole('button', { name: /돌아가기/ }));
        await user.click(screen.getByRole('button', { name: '매매 중지' }));
        const stop_dialog = await screen.findByRole('dialog', { name: '매매를 중지할까요?' });
        await user.click(within(stop_dialog).getByRole('button', { name: '매매 중지' }));

        // 중지 완료 후 다시 시작할 수 있는 상태로 돌아오는지 확인한다.
        await waitFor(() => {
            expect(screen.getByRole('button', { name: '자동매매 실행' })).toBeEnabled();
        });
    });
});
