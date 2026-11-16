# Native boundaries used only by the disposable-host harness.
Add-Type @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;
using Microsoft.Win32.SafeHandles;
public static class MdmWindows {
    [StructLayout(LayoutKind.Sequential)] struct Attributes {
        public uint Length;
        public IntPtr Root, Name;
        public uint Flags;
        public IntPtr Descriptor, Quality;
    }
    [StructLayout(LayoutKind.Sequential)] struct UnicodeString {
        public ushort Length, MaximumLength;
        public IntPtr Buffer;
    }
    [DllImport("advapi32.dll")] static extern uint LsaOpenPolicy(IntPtr system, ref Attributes attrs, uint access, out IntPtr policy);
    [DllImport("advapi32.dll")] static extern uint LsaAddAccountRights(IntPtr policy, byte[] sid, UnicodeString[] rights, uint count);
    [DllImport("advapi32.dll")] static extern uint LsaRemoveAccountRights(IntPtr policy, byte[] sid, [MarshalAs(UnmanagedType.U1)] bool all, UnicodeString[] rights, uint count);
    [DllImport("advapi32.dll")] static extern uint LsaClose(IntPtr policy);
    [DllImport("advapi32.dll")] static extern uint LsaNtStatusToWinError(uint status);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetNamedPipeClientProcessId(SafePipeHandle pipe, out uint pid);
    static void Check(uint status) {
        if (status != 0) throw new Win32Exception((int)LsaNtStatusToWinError(status));
    }
    public static void BatchLogon(string sid, bool enable) {
        var attrs = new Attributes();
        attrs.Length = (uint)Marshal.SizeOf(attrs);
        IntPtr policy;
        Check(LsaOpenPolicy(IntPtr.Zero, ref attrs, 0x810, out policy));
        const string name = "SeBatchLogonRight";
        IntPtr text = Marshal.StringToHGlobalUni(name);
        try {
            var account = new SecurityIdentifier(sid);
            var bytes = new byte[account.BinaryLength];
            account.GetBinaryForm(bytes, 0);
            var right = new UnicodeString { Length = (ushort)(name.Length * 2), MaximumLength = (ushort)((name.Length + 1) * 2), Buffer = text };
            if (enable) Check(LsaAddAccountRights(policy, bytes, new[] { right }, 1));
            else Check(LsaRemoveAccountRights(policy, bytes, false, new[] { right }, 1));
        } finally { Marshal.FreeHGlobal(text); LsaClose(policy); }
    }
    public static uint PipeClient(SafePipeHandle pipe) {
        uint pid;
        if (!GetNamedPipeClientProcessId(pipe, out pid)) throw new Win32Exception(Marshal.GetLastWin32Error());
        return pid;
    }
}
'@
