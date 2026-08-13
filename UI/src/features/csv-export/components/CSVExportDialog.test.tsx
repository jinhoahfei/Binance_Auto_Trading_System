import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { CSVExportDialog, DEFAULT_CSV_EXPORT_DRAFT } from './index';
import type { CSVExportDialogProps } from './CSVExportDialog';

/**
 * 함수 이름: create_dialog_props()
 * 기능: CSV 내보내기 팝업 테스트에 사용할 제어형 기본 속성과 이벤트 감시 함수를 만든다.
 * 인자: 없음
 * 반환값: CSV 내보내기 팝업 속성과 각 의도 감시 함수
 * 작성 날짜: 2026/08/12
 */
function create_dialog_props() {
  const handlers = {
    onDismiss: vi.fn(),
    onChooseLocation: vi.fn(),
    onPeriodChange: vi.fn(),
    onCalendarOpen: vi.fn(),
    onCalendarDismiss: vi.fn(),
    onCalendarDateSelect: vi.fn(),
    onCalendarPreviousMonth: vi.fn(),
    onCalendarNextMonth: vi.fn(),
    onCalendarMonthChange: vi.fn(),
    onCalendarYearChange: vi.fn(),
    onFileNameChange: vi.fn(),
    onExport: vi.fn(),
  };
  const props: CSVExportDialogProps = {
    open: true,
    draft: DEFAULT_CSV_EXPORT_DRAFT,
    ...handlers,
  };

  return { handlers, props };
}

describe('CSVExportDialog', () => {
  it('저장 위치, 기간, 파일명과 내보내기 의도를 전달한다', async () => {
    const user = userEvent.setup();
    const { handlers, props } = create_dialog_props();

    render(<CSVExportDialog {...props} />);

    expect(screen.getByRole('dialog', { name: 'CSV 내보내기' })).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: /달력/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '위치 선택' }));
    await user.click(screen.getByRole('button', { name: '최근 30일' }));
    fireEvent.change(screen.getByLabelText('파일 이름'), { target: { value: 'history.csv' } });
    await user.click(screen.getByRole('button', { name: '내보내기' }));

    expect(handlers.onChooseLocation).toHaveBeenCalledTimes(1);
    expect(handlers.onPeriodChange).toHaveBeenCalledWith('MONTHLY');
    expect(handlers.onFileNameChange).toHaveBeenLastCalledWith('history.csv');
    expect(handlers.onExport).toHaveBeenCalledTimes(1);
  });

  it('시작일 달력에서 날짜와 월 이동 의도를 대상과 함께 전달한다', async () => {
    const user = userEvent.setup();
    const { handlers, props } = create_dialog_props();

    render(
      <CSVExportDialog
        {...props}
        calendar={{ year: 2026, month: 8, selectedDate: '2026-08-12' }}
        calendarTarget="START"
        draft={{ ...DEFAULT_CSV_EXPORT_DRAFT, period: 'CUSTOM' }}
      />,
    );

    expect(screen.getByRole('dialog', { name: '시작일 선택 달력' })).toBeInTheDocument();
    await user.click(screen.getByRole('gridcell', { name: '2026년 8월 12일 선택' }));
    await user.click(screen.getByRole('button', { name: '다음 달' }));
    await user.selectOptions(screen.getByRole('combobox', { name: '달력 월' }), '6');

    expect(handlers.onCalendarDateSelect).toHaveBeenCalledWith('START', '2026-08-12');
    expect(handlers.onCalendarNextMonth).toHaveBeenCalledWith('START');
    expect(handlers.onCalendarMonthChange).toHaveBeenCalledWith('START', 6);
  });

  it('달력이 열린 동안 배경을 누르면 달력만 닫고 이후에는 모달을 닫는다', () => {
    const { handlers, props } = create_dialog_props();
    const { rerender } = render(
      <CSVExportDialog
        {...props}
        calendar={{ year: 2026, month: 8, selectedDate: '2026-08-12' }}
        calendarTarget="END"
        draft={{ ...DEFAULT_CSV_EXPORT_DRAFT, period: 'CUSTOM' }}
      />,
    );

    fireEvent.mouseDown(screen.getByTestId('csv-export-overlay'));
    expect(handlers.onCalendarDismiss).toHaveBeenCalledTimes(1);
    expect(handlers.onDismiss).not.toHaveBeenCalled();

    rerender(<CSVExportDialog {...props} calendarTarget={null} />);
    fireEvent.mouseDown(screen.getByTestId('csv-export-overlay'));
    expect(handlers.onDismiss).toHaveBeenCalledTimes(1);
  });

  it('저장 위치와 날짜 오류를 각각 경고로 표시한다', () => {
    const { props } = create_dialog_props();

    render(
      <CSVExportDialog
        {...props}
        errors={{
          saveLocation: '저장 위치를 선택해야 합니다',
          dateRange: '시작일이 종료일보다 늦을 수 없습니다',
        }}
      />,
    );

    expect(screen.getByText(/저장 위치를 선택해야 합니다/)).toHaveAttribute('role', 'alert');
    expect(screen.getByText(/시작일이 종료일보다 늦을 수 없습니다/)).toHaveAttribute('role', 'alert');
  });

  it('모달이 제거되면 모달을 열었던 요소로 초점을 복원한다', async () => {
    const { props } = create_dialog_props();
    const trigger = document.createElement('button');

    trigger.textContent = 'CSV 열기';
    document.body.append(trigger);
    trigger.focus();

    const { unmount } = render(<CSVExportDialog {...props} />);

    unmount();

    await waitFor(() => expect(trigger).toHaveFocus());
    trigger.remove();
  });
});
