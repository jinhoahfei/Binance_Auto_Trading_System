const DECIMAL_TEXT_PATTERN = /^([+-]?)(0|[1-9][0-9]*)(\.[0-9]+)?$/u;

/**
 * 함수 이름: format_decimal_text()
 * 기능: 금융 Decimal 문자열을 JS Number 변환 없이 천 단위 구분 표시로 변환한다.
 * 인자: decimal_text -> backend 또는 demo fixture가 제공한 plain Decimal 문자열
 * 반환값: 부호와 소수 자릿수를 그대로 보존한 표시 문자열
 * 작성 날짜: 2026/08/21
 */
export function format_decimal_text(decimal_text: string): string {
    const matched_parts = DECIMAL_TEXT_PATTERN.exec(decimal_text);

    if (matched_parts === null) {
        return decimal_text;
    }

    const sign = matched_parts[1] ?? '';
    const integer_part = matched_parts[2] ?? '0';
    const fractional_part = matched_parts[3] ?? '';
    const grouped_integer = integer_part.replace(/\B(?=(\d{3})+(?!\d))/gu, ',');

    return `${sign}${grouped_integer}${fractional_part}`;
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
    const is_zero = /^-?0(?:\.0+)?$/u.test(decimal_text);

    return decimal_text.startsWith('-') || is_zero
        ? formatted_amount
        : `+ ${formatted_amount}`;
}
