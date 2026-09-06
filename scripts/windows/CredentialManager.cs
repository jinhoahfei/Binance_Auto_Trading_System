using System;
using System.Runtime.InteropServices;
using System.Security;

// 클래스 이름: CredentialManager
// 기능: 고정 Testnet target의 Windows generic credential을 secret 출력 없이 조작한다.
// 작성 날짜: 2026/09/06
public static class CredentialManager
{
    private const string target_prefix = "com.binance-auto.trader.testnet/";
    private const uint generic_type = 1;
    private const int maximum_secret_bytes = 512;

    // 클래스 이름: NativeCredential
    // 기능: CREDENTIALW의 Windows ABI layout을 P/Invoke에 제공한다.
    // 작성 날짜: 2026/09/06
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NativeCredential
    {
        public uint flags;
        public uint type;
        public string target_name;
        public string comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME last_written;
        public uint blob_size;
        public IntPtr blob;
        public uint persist;
        public uint attribute_count;
        public IntPtr attributes;
        public string target_alias;
        public string user_name;
    }

    // Native API 선언은 Unicode entrypoint와 error 전달을 명시해 ANSI 자동 선택을 막는다.
    // 함수 이름: cred_write()
    // 기능: CREDENTIALW를 Windows generic store에 저장한다.
    // 인자: credential -> native 구조체, flags -> API 예약값 0
    // 반환값: native 성공 여부
    // 작성 날짜: 2026/09/06
    [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool cred_write(ref NativeCredential credential, uint flags);

    // 함수 이름: cred_read()
    // 기능: 고정 target의 native credential buffer를 조회한다.
    // 인자: target/type -> credential identity, flags -> 0, credential -> 반환 pointer
    // 반환값: native 성공 여부
    // 작성 날짜: 2026/09/06
    [DllImport("advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool cred_read(string target, uint type, uint flags, out IntPtr credential);

    // 함수 이름: cred_delete()
    // 기능: 고정 target의 native credential을 삭제한다.
    // 인자: target/type -> credential identity, flags -> 0
    // 반환값: native 성공 여부
    // 작성 날짜: 2026/09/06
    [DllImport("advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool cred_delete(string target, uint type, uint flags);

    // 함수 이름: cred_free()
    // 기능: CredRead가 할당한 credential buffer를 OS에 반환한다.
    // 인자: buffer -> API가 반환한 native pointer
    // 반환값: 없음
    // 작성 날짜: 2026/09/06
    [DllImport("advapi32.dll", EntryPoint = "CredFree")]
    private static extern void cred_free(IntPtr buffer);

    // 함수 이름: get_target()
    // 기능: 도구가 사용하는 세 고정 account 밖의 credential 접근을 거부한다.
    // 인자: account -> api-key, api-secret 또는 별도 canary
    // 반환값: 고정 generic target
    // 작성 날짜: 2026/09/06
    private static string get_target(string account)
    {
        if (account != "api-key" && account != "api-secret" && account != "session5-canary")
            throw new InvalidOperationException("Unsupported credential account.");
        return target_prefix + account;  // Renderer나 environment가 target을 선택하지 않는다.
    }

    // 함수 이름: validate_secret()
    // 기능: hidden input을 trim하지 않고 bounded printable ASCII인지 검사한다.
    // 인자: secret -> 호출자가 소유하는 SecureString
    // 반환값: 유효하면 정상 반환
    // 작성 날짜: 2026/09/06
    public static void validate_secret(SecureString secret)
    {
        if (secret == null || secret.Length == 0 || secret.Length > maximum_secret_bytes)
            throw new InvalidOperationException("Invalid credential input.");

        // Managed immutable plaintext string을 만들지 않고 BSTR memory에서 문자만 검사한다.
        IntPtr unicode_secret = Marshal.SecureStringToBSTR(secret);
        try
        {
            for (int index = 0; index < secret.Length; index++)
            {
                int character = (ushort)Marshal.ReadInt16(unicode_secret, index * 2);
                if (character < 0x21 || character > 0x7e)
                    throw new InvalidOperationException("Invalid credential input.");
            }
        }
        finally { Marshal.ZeroFreeBSTR(unicode_secret); }
    }

    // 함수 이름: write_secret()
    // 기능: SecureString을 Rust와 동일한 ASCII blob으로 저장하고 임시 메모리를 덮어쓴다.
    // 인자: account -> 고정 account, secret -> 숨김 입력
    // 반환값: 성공하면 정상 반환
    // 작성 날짜: 2026/09/06
    public static void write_secret(string account, SecureString secret)
    {
        string target = get_target(account);
        validate_secret(secret);
        IntPtr unicode_secret = IntPtr.Zero;
        IntPtr ascii_secret = IntPtr.Zero;
        try
        {
            // ASCII는 generic blob의 application-defined encoding이며 NUL을 포함하지 않는다.
            unicode_secret = Marshal.SecureStringToBSTR(secret);
            ascii_secret = Marshal.AllocHGlobal(secret.Length);
            for (int index = 0; index < secret.Length; index++)
                Marshal.WriteByte(ascii_secret, index, (byte)Marshal.ReadInt16(unicode_secret, index * 2));
            NativeCredential credential = new NativeCredential {
                type = generic_type, target_name = target, blob = ascii_secret,
                blob_size = (uint)secret.Length, persist = 2, user_name = account
            };
            if (!cred_write(ref credential, 0))
                throw new InvalidOperationException("Credential write failed.");
        }
        finally
        {
            // 모든 성공·실패 경로에서 secret buffer를 free 전에 zeroize한다.
            if (unicode_secret != IntPtr.Zero) Marshal.ZeroFreeBSTR(unicode_secret);
            if (ascii_secret != IntPtr.Zero)
            {
                for (int index = 0; index < secret.Length; index++) Marshal.WriteByte(ascii_secret, index, 0);
                Marshal.FreeHGlobal(ascii_secret);
            }
        }
    }

    // 함수 이름: verify_secret()
    // 기능: 저장된 blob의 계약과 선택적 canary 일치를 확인하되 값을 반환하지 않는다.
    // 인자: account -> 고정 account, expected -> null 또는 canary SecureString
    // 반환값: 계약과 선택적 기대값을 만족하면 true
    // 작성 날짜: 2026/09/06
    public static bool verify_secret(string account, SecureString expected)
    {
        IntPtr pointer;
        if (!cred_read(get_target(account), generic_type, 0, out pointer))
        {
            if (Marshal.GetLastWin32Error() == 1168) return false;
            throw new InvalidOperationException("Credential read failed.");
        }
        NativeCredential credential = new NativeCredential();
        IntPtr expected_pointer = IntPtr.Zero;
        try
        {
            // Read 결과는 native buffer에서만 검사하고 managed byte array/string으로 복사하지 않는다.
            credential = (NativeCredential)Marshal.PtrToStructure(pointer, typeof(NativeCredential));
            if (credential.type != generic_type || credential.blob == IntPtr.Zero ||
                credential.blob_size == 0 || credential.blob_size > maximum_secret_bytes) return false;
            if (expected != null)
            {
                if (expected.Length != credential.blob_size) return false;
                expected_pointer = Marshal.SecureStringToBSTR(expected);
            }
            int mismatch = 0;
            for (int index = 0; index < credential.blob_size; index++)
            {
                byte character = Marshal.ReadByte(credential.blob, index);
                if (character < 0x21 || character > 0x7e) mismatch |= 1;
                if (expected_pointer != IntPtr.Zero)
                    mismatch |= character ^ (ushort)Marshal.ReadInt16(expected_pointer, index * 2);
            }
            return mismatch == 0;
        }
        finally
        {
            if (expected_pointer != IntPtr.Zero) Marshal.ZeroFreeBSTR(expected_pointer);
            // OS API의 2560-byte 최대 blob 계약 안에서 읽은 원본도 zeroize 후 CredFree한다.
            if (credential.blob != IntPtr.Zero && credential.blob_size <= 2560)
                for (int index = 0; index < credential.blob_size; index++) Marshal.WriteByte(credential.blob, index, 0);
            cred_free(pointer);
        }
    }

    // 함수 이름: delete_secret()
    // 기능: 고정 credential을 삭제하고 이미 없는 경우만 멱등 성공으로 처리한다.
    // 인자: account -> 고정 account
    // 반환값: 성공하면 정상 반환
    // 작성 날짜: 2026/09/06
    public static void delete_secret(string account)
    {
        if (!cred_delete(get_target(account), generic_type, 0) && Marshal.GetLastWin32Error() != 1168)
            throw new InvalidOperationException("Credential delete failed.");
    }
}
