import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { FakeUiCommandAdapter } from '../../shared/testing';
import { UiApplicationFacade } from '../control';
import { AppModalHost } from './AppModalHost';

describe('AppModalHost Phase 13 CSV status trace', () => {
    /** Communication Case 4 메시지 4.1.3b의 progress와 non-progress 표면을 검증한다. */
    it('test_csv_progress_surface_and_non_progress_absence: 진행 상태만 처리 중으로 표시한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), {
            today: '2026-08-25',
        });
        const base_view_model = facade.get_view_model();
        const { rerender } = render(
            <AppModalHost
                controller={facade}
                viewModel={{
                    ...base_view_model,
                    active_modal: 'csv_export_progress',
                    csv_export: {
                        ...base_view_model.csv_export,
                        file_name: 'phase13-history.csv',
                    },
                }}
            />,
        );

        // 화면의 표시 내용과 입력 가능 상태를 검증한다.
        expect(screen.getByRole('dialog', { name: 'CSV 내보내기' })).toBeInTheDocument();
        expect(screen.getByText('처리 중')).toBeInTheDocument();
        expect(screen.getByText('phase13-history.csv')).toBeInTheDocument();

        // Modal slot이 해제되면 stale progress surface가 DOM에 남지 않아야 한다.
        rerender(
            <AppModalHost
                controller={facade}
                viewModel={{ ...base_view_model, active_modal: null }}
            />,
        );
        expect(screen.queryByRole('dialog', { name: 'CSV 내보내기' })).not.toBeInTheDocument();
    });

    /** Communication Case 4 메시지 4.1.6a의 export-error 표면을 직접 검증한다. */
    it('test_csv_export_error_surface: 실패 이유를 완료와 구분해 표시한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), {
            today: '2026-08-25',
        });
        const base_view_model = facade.get_view_model();

        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <AppModalHost
                controller={facade}
                viewModel={{
                    ...base_view_model,
                    active_modal: 'csv_export_error',
                    csv_export: {
                        ...base_view_model.csv_export,
                        command_error: {
                            code: 'CSV_EXPORT_IO_FAILED',
                            message: 'safe write failure',
                        },
                    },
                }}
            />,
        );

        // Error branch는 실패 title과 detail을 표시하고 성공 title을 동시에 만들지 않는다.
        expect(screen.getByRole('dialog', { name: 'CSV 내보내기 실패' })).toBeInTheDocument();
        expect(screen.getByText('오류')).toBeInTheDocument();
        expect(screen.getByText('safe write failure')).toBeInTheDocument();
        expect(screen.queryByRole('dialog', { name: 'CSV 내보내기 완료' })).not.toBeInTheDocument();
    });

    /** Communication Case 4 메시지 4.1.6b의 완료와 non-completion 표면을 검증한다. */
    it('test_csv_export_complete_surface_and_error_absence: 성공 경로만 저장 결과를 표시한다', () => {
        // 시나리오에 필요한 입력과 테스트용 의존성을 준비한다.
        const facade = new UiApplicationFacade(new FakeUiCommandAdapter(), {
            today: '2026-08-25',
        });
        const base_view_model = facade.get_view_model();

        // 준비한 의존성을 주입해 화면 또는 hook을 실행한다.
        render(
            <AppModalHost
                controller={facade}
                viewModel={{
                    ...base_view_model,
                    active_modal: 'csv_export_complete',
                    csv_export: {
                        ...base_view_model.csv_export,
                        receipt_path: '/safe/phase13-history.csv',
                    },
                }}
            />,
        );

        // 완료 branch는 receipt path를 표시하며 error/progress surface를 함께 노출하지 않는다.
        expect(screen.getByRole('dialog', { name: 'CSV 내보내기 완료' })).toBeInTheDocument();
        expect(screen.getByText('완료')).toBeInTheDocument();
        expect(screen.getByText('/safe/phase13-history.csv')).toBeInTheDocument();
        expect(screen.queryByRole('dialog', { name: 'CSV 내보내기 실패' })).not.toBeInTheDocument();
        expect(screen.queryByText('처리 중')).not.toBeInTheDocument();
    });
});
