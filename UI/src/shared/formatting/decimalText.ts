const DECIMAL_TEXT_PATTERN = /^([+-]?)(0|[1-9][0-9]*)(\.[0-9]+)?$/u;

/**
 * 함수 이름: format_decimal_text()
 * 기능: 금융 Decimal 문자열을 JS Number 변환 없이 반올림하고 천 단위 구분을 적용한다.
 * 인자: decimal_text -> backend 또는 demo fixture가 제공한 plain Decimal 문자열
 *      decimal_places -> 표시할 소수 자릿수, 일반 숫자는 2자리, ETH 수량은 4자리
 * 반환값: 지정 자릿수로 반올림한 표시 문자열, 반올림된 0은 부호를 생략한다.
 * 작성 날짜: 2026/08/21
 */
export function format_decimal_text(decimal_text: string, decimal_places: 2 | 4 = 2): string {
    const matched_parts = DECIMAL_TEXT_PATTERN.exec(decimal_text);

    if (matched_parts === null) {
        return decimal_text;
    }

    const integer_part = matched_parts[2] ?? '0';
    const fractional_part = (matched_parts[3] ?? '').slice(1);
    const retained_fraction = fractional_part.slice(0, decimal_places).padEnd(decimal_places, '0');
    let rounded_units = BigInt(`${integer_part}${retained_fraction}`);

    // 절댓값의 다음 자리가 5 이상이면 올려 음수와 큰 금액도 정확하게 반올림한다.
    if ((fractional_part[decimal_places] ?? '0') >= '5') {
        rounded_units += 1n;
    }

    const rounded_digits = rounded_units.toString().padStart(decimal_places + 1, '0');
    const rounded_integer = rounded_digits.slice(0, -decimal_places);
    const rounded_fraction = rounded_digits.slice(-decimal_places);
    const grouped_integer = rounded_integer.replace(/\B(?=(\d{3})+(?!\d))/gu, ',');
    const sign = rounded_units === 0n ? '' : matched_parts[1] ?? '';

    return `${sign}${grouped_integer}.${rounded_fraction}`;
}

/**
 * 함수 이름: format_eth_quantity()
 * 기능: ETH 보유량과 체결 수량을 소수점 넷째 자리까지 반올림해 표시한다.
 * 인자: decimal_text -> ETH 수량 Decimal 문자열
 * 반환값: 소수점 4자리의 수량 표시 문자열
 * 작성 날짜: 2026/09/05
 */
export function format_eth_quantity(decimal_text: string): string {
    return format_decimal_text(decimal_text, 4);
}

/**
 * 함수 이름: format_quote_amount()
 * 기능: live quote asset은 명시적 단위로, 기존 demo 값은 원화 기호로 표시한다.
 * 인자: decimal_text -> 표시할 quote 금액 Decimal 문자열
 *      quote_asset -> live 거래의 quote asset 또는 demo fixture의 undefined
 * 반환값: quote asset 의미를 보존한 금액 문자열
 * 작성 날짜: 2026/08/21
 */
export function format_quote_amount(
    decimal_text: string,
    quote_asset: string | undefined,
): string {
    const formatted_amount = format_decimal_text(decimal_text);

    return quote_asset === undefined
        ? `₩${formatted_amount}`
        : `${formatted_amount} ${quote_asset}`;
}

/**
 * 함수 이름: format_signed_quote_amount()
 * 기능: Decimal 문자열의 기존 부호를 보존하고 양수에만 명시적 plus 표시를 붙인다.
 * 인자: decimal_text -> 표시할 quote 금액 Decimal 문자열
 *      quote_asset -> live quote asset 또는 demo의 undefined
 * 반환값: JS Number 계산 없이 sign-safe하게 포맷된 금액
 * 작성 날짜: 2026/08/21
 */
export function format_signed_quote_amount(
    decimal_text: string,
    quote_asset: string | undefined,
): string {
    const formatted_amount = format_quote_amount(decimal_text, quote_asset);
    const is_zero = format_decimal_text(decimal_text) === '0.00';

    return /^[+-]/u.test(decimal_text) || is_zero
        ? formatted_amount
        : `+ ${formatted_amount}`;
}
