import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
    const calendar_rows = within(
      screen.getByRole('dialog', { name: '시작일 선택 달력' }),
    ).getAllByRole('row');

    // 선행·후행 빈 칸도 gridcell로 보존해 각 주의 요일 column 수가 항상 일곱인지 검증한다.
    for (const calendar_row of calendar_rows) {
      expect(within(calendar_row).getAllByRole('gridcell')).toHaveLength(7);
    }
    await user.click(screen.getByRole('gridcell', { name: '2026년 8월 12일 선택' }));
    await user.click(screen.getByRole('button', { name: '다음 달' }));
    await user.selectOptions(screen.getByRole('combobox', { name: '달력 월' }), '6');

    expect(handlers.onCalendarDateSelect).toHaveBeenCalledWith('START', '2026-08-12');
    expect(handlers.onCalendarNextMonth).toHaveBeenCalledWith('START');
    expect(handlers.onCalendarMonthChange).toHaveBeenCalledWith('START', 6);
  });

  /** Static Figma 강조가 실제 custom date의 제어·접근성 상태를 훼손하지 않는지 검증한다. */
  it('fixture 기간 강조와 달력 header 날짜를 선택 원과 독립적으로 표시한다', () => {
    const { props } = create_dialog_props();

    render(
      <CSVExportDialog
        {...props}
        calendar={{
          year: 2026,
          month: 6,
          selectedDate: '2026-06-29',
          headerDate: '2026-06-22',
        }}
        calendarTarget="END"
        draft={{ ...DEFAULT_CSV_EXPORT_DRAFT, period: 'CUSTOM' }}
        visualPeriod="TODAY"
      />,
    );

    const today_button = screen.getByRole('button', { name: '오늘' });
    const custom_button = screen.getByRole('button', { name: '날짜 선택' });

    // Figma 강조는 오늘에 남겨도 assistive technology에는 실제 CUSTOM draft를 전달한다.
    expect(today_button.className).not.toBe('');
    expect(custom_button.className).toBe('');
    expect(today_button).toHaveAttribute('aria-pressed', 'false');
    expect(custom_button).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('2026.06.22')).toBeInTheDocument();
    expect(screen.getByRole('gridcell', { name: '2026년 6월 29일 선택' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
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
    expect(screen.getByRole('button', { name: /시작일 선택/ })).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('button', { name: /종료일 선택/ })).toHaveAttribute('aria-invalid', 'true');
  });

  /** Static Figma presentation이 날짜 검증의 접근성 의미를 없애지 않는지 검증한다. */
  it('fixture는 invalid border만 중립화하고 오류 의미를 유지한다', () => {
    const { props } = create_dialog_props();

    render(
      <CSVExportDialog
        {...props}
        errors={{ dateRange: '시작일이 종료일보다 늦을 수 없습니다' }}
        suppressDateRangeInvalidBorder
      />,
    );

    // 시각 떨림과 무관하게 assistive technology에는 두 날짜가 계속 invalid로 전달된다.
    expect(screen.getByRole('button', { name: /시작일 선택/ })).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('button', { name: /종료일 선택/ })).toHaveAttribute('aria-invalid', 'true');
  });

  /** Communication Case 4 메시지 2·3·4의 중복 입력 차단 경계를 검증한다. */
  it('test_exporting_dialog_disables_picker_edits_and_duplicate_confirm: 진행 중 입력을 무시한다', async () => {
    const user = userEvent.setup();
    const { handlers, props } = create_dialog_props();

    render(<CSVExportDialog {...props} exporting />);

    // Exporting 상태의 모든 mutation control은 native disabled라 callback을 만들 수 없다.
    const location_button = screen.getByRole('button', { name: '위치 선택' });
    const period_button = screen.getByRole('button', { name: '최근 30일' });
    const file_name_input = screen.getByLabelText('파일 이름');
    const export_button = screen.getByRole('button', { name: '내보내는 중' });
    await user.click(location_button);
    await user.click(period_button);
    await user.type(file_name_input, 'mutated.csv');
    await user.click(export_button);

    expect(location_button).toBeDisabled();
    expect(period_button).toBeDisabled();
    expect(file_name_input).toBeDisabled();
    expect(export_button).toBeDisabled();
    expect(handlers.onChooseLocation).not.toHaveBeenCalled();
    expect(handlers.onPeriodChange).not.toHaveBeenCalled();
    expect(handlers.onFileNameChange).not.toHaveBeenCalled();
    expect(handlers.onExport).not.toHaveBeenCalled();
  });

  /** Communication Case 4 메시지 4.1.3a가 valid 상태에 남지 않는지 검증한다. */
  it('test_valid_csv_dialog_has_no_validation_error_surface: 유효 상태는 오류를 표시하지 않는다', () => {
    const { props } = create_dialog_props();

    render(<CSVExportDialog {...props} errors={{}} />);

    // 오류가 없는 draft에는 validation alert와 invalid attribute를 만들지 않는다.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByLabelText('파일 이름')).toHaveAttribute('aria-invalid', 'false');
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
