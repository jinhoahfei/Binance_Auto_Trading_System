import { format_decimal_text, format_eth_quantity, format_signed_quote_amount } from './decimalText';

describe('금융 숫자 표시', () => {
    it.each([
        ['2451.42000000', '2,451.42'],
        ['9999.97871300', '9,999.98'],
        ['2472.082979765123456789', '2,472.08'],
        ['25', '25.00'],
        ['-0.04275529', '-0.04'],
        ['1.005', '1.01'],
        ['-1.005', '-1.01'],
        ['+1.005', '+1.01'],
        ['999.995', '1,000.00'],
        ['99999999999999999999.995', '100,000,000,000,000,000,000.00'],
        ['0.000000000000000000', '0.00'],
        ['-0.00040793', '0.00'],
        ['+0.0049', '0.00'],
        ['-', '-'],
        ['--', '--'],
    ])('%s를 소수점 2자리로 정확하게 반올림한다', (input, expected) => {
        expect(format_decimal_text(input)).toBe(expected);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    });

    it.each([
        ['1.00000000', '1.0000'],
        ['0.00420000', '0.0042'],
        ['0.00414999', '0.0041'],
        ['0.00415000', '0.0042'],
        ['999.99995', '1,000.0000'],
        ['-0.00415', '-0.0042'],
        ['-0.00001', '0.0000'],
        ['0', '0.0000'],
        ['-', '-'],
    ])('ETH 수량 %s를 소수점 4자리로 반올림한다', (input, expected) => {
        expect(format_eth_quantity(input)).toBe(expected);  // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
    });

    it('반올림한 금액의 단위와 부호를 표시하고 0이나 기존 양수 부호를 중복 장식하지 않는다', () => {
        // 반환값과 관찰한 상태가 시나리오의 기대값과 일치하는지 검증한다.
        expect(format_signed_quote_amount('1.005', 'USDT')).toBe('+ 1.01 USDT');
        expect(format_signed_quote_amount('-1.005', 'USDT')).toBe('-1.01 USDT');
        expect(format_signed_quote_amount('+1.005', 'USDT')).toBe('+1.01 USDT');
        expect(format_signed_quote_amount('0.004', 'USDT')).toBe('0.00 USDT');
        expect(format_signed_quote_amount('-0.004', 'USDT')).toBe('0.00 USDT');
        expect(format_signed_quote_amount('1.005', undefined)).toBe('+ ₩1.01');
    });
});
