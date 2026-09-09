using System;
using System.Text;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

[ComImport, Guid("000214F9-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IShellLinkW
{
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int size, IntPtr findData, uint flags);
    void GetIDList(out IntPtr list);
    void SetIDList(IntPtr list);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int size);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string value);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int size);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string value);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int size);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string value);
    void GetHotkey(out short value);
    void SetHotkey(short value);
    void GetShowCmd(out int value);
    void SetShowCmd(int value);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder value, int size, out int index);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string value, int index);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
    void Resolve(IntPtr window, uint flags);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
}

public static class LabControlShortcut
{
    private static object NewLink()
    {
        return Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("00021401-0000-0000-C000-000000000046")));
    }

    public static void Create(string shortcut, string target)
    {
        object obj = NewLink();
        try
        {
            var link = (IShellLinkW)obj;
            link.SetPath(target);
            link.SetDescription("Lab Control Center");
            link.SetRelativePath(shortcut, 0);
            // No interpreter argument, working directory or separate icon path.
            ((IPersistFile)obj).Save(shortcut, true);
        }
        finally { Marshal.FinalReleaseComObject(obj); }
    }

    public static string ResolvedTarget(string shortcut)
    {
        object obj = NewLink();
        try
        {
            ((IPersistFile)obj).Load(shortcut, 0);
            var link = (IShellLinkW)obj;
            link.Resolve(IntPtr.Zero, 0x31); // No UI, search or distributed tracking.
            var path = new StringBuilder(4096);
            link.GetPath(path, path.Capacity, IntPtr.Zero, 0);
            return path.ToString();
        }
        finally { Marshal.FinalReleaseComObject(obj); }
    }
}
