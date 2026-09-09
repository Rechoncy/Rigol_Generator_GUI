// Build with tools/build_windows_launcher.ps1; no developer-machine paths.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Forms;
using Microsoft.Win32;

internal static class LabControlLauncher
{
    private static readonly string Root = AppDomain.CurrentDomain.BaseDirectory;
    private static readonly StringBuilder Log = new StringBuilder();
    private static string LogPath;

    [STAThread]
    private static int Main(string[] args)
    {
        bool silent = args.Contains("--launcher-no-dialog");
        args = args.Where(a => a != "--launcher-no-dialog").ToArray();
        try
        {
            string logDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                         "RigolGeneratorControl", "logs");
            Directory.CreateDirectory(logDir);
            LogPath = Path.Combine(logDir, "launcher-" + DateTime.Now.ToString("yyyyMMdd-HHmmss-fff") +
                                   "-" + Process.GetCurrentProcess().Id + ".log");
            Log.AppendLine("Project: " + Root);
            string script = Path.Combine(Root, "tools", "launch_app.py");
            if (!File.Exists(script) || !File.Exists(Path.Combine(Root, "src", "rigol_control", "app.py")))
                throw new Exception("项目文件不完整。请复制整个项目文件夹，不能只复制 EXE 或快捷方式。");

            string python = FindPython();
            Log.AppendLine("Selected Python: " + python);
            SaveLog();
            var start = StartInfo(python, Quote(script) + " " + string.Join(" ", args.Select(Quote)));
            start.EnvironmentVariables["PYTHONUTF8"] = "1";
            // Do not inherit another project's PYTHONPATH/PYTHONHOME.
            start.EnvironmentVariables.Remove("PYTHONHOME");
            start.EnvironmentVariables["PYTHONPATH"] = Path.Combine(Root, "src");
            using (var process = Process.Start(start))
            {
                var output = process.StandardOutput.ReadToEndAsync();
                var error = process.StandardError.ReadToEndAsync();
                process.WaitForExit();
                Log.AppendLine(output.Result);
                Log.AppendLine(error.Result);
                Log.AppendLine("Application exit code: " + process.ExitCode);
                SaveLog();
                if (process.ExitCode != 0)
                    throw new Exception("软件启动或运行失败，详细错误已写入日志。\n" + error.Result);
            }
            return 0;
        }
        catch (Exception ex)
        {
            Log.AppendLine(ex.ToString());
            SaveLog();
            if (!silent)
                MessageBox.Show(ex.Message + "\n\n日志：" + (LogPath ?? "无法创建日志"),
                                "Lab Control Center 启动失败", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static string FindPython()
    {
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (string candidate in Candidates())
        {
            if (string.IsNullOrWhiteSpace(candidate)) continue;
            string path = Environment.ExpandEnvironmentVariables(candidate.Trim().Trim('"'));
            if (!File.Exists(path) || !seen.Add(path)) continue;
            // Windows Store execution aliases may open the store instead of Python.
            if (path.IndexOf(@"\Microsoft\WindowsApps\", StringComparison.OrdinalIgnoreCase) >= 0) continue;
            string output;
            string probe = "import sys,struct; assert sys.version_info >= (3,10), 'Python 3.10+ required'; " +
                           "assert struct.calcsize('P') == 8, '64-bit Python required'; " +
                           "from PyQt6 import QtCore,QtGui,QtWidgets; print(sys.executable)";
            if (RunProbe(path, "-c " + Quote(probe), out output)) return output.Trim();
        }
        throw new Exception("未找到可用的 64 位 Python 3.10+ 和 PyQt6。\n" +
            "请在这台电脑的 Python 环境中执行：\npython -m pip install PyQt6 pyserial PyVISA nidaqmx\n\n" +
            "已有独立环境时，可将 LAB_CONTROL_PYTHON 环境变量设为该环境 python.exe 的完整路径。\n" +
            "NI-DAQmx / VISA 等厂商驱动仍需在这台电脑上安装。");
    }

    private static IEnumerable<string> Candidates()
    {
        yield return Environment.GetEnvironmentVariable("LAB_CONTROL_PYTHON");
        foreach (string local in new[] { @"runtime\python.exe", @"python\python.exe",
                                        @".venv\Scripts\python.exe", @"venv\Scripts\python.exe" })
            yield return Path.Combine(Root, local);
        foreach (string dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
            if (!string.IsNullOrWhiteSpace(dir)) yield return Path.Combine(dir.Trim().Trim('"'), "python.exe");

        string list;
        if (RunProbe("py.exe", "-0p", out list))
            foreach (string line in list.Split('\n'))
            {
                Match match = Regex.Match(line, @"([A-Za-z]:\\.*python(?:w)?\.exe)\s*$", RegexOptions.IgnoreCase);
                if (match.Success) yield return match.Groups[1].Value;
            }
        foreach (RegistryKey hive in new[] { Registry.CurrentUser, Registry.LocalMachine })
            foreach (string path in RegistryPythons(hive)) yield return path;

        string user = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        foreach (string parent in new[] { user, Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                          Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData) })
            foreach (string name in new[] { "miniconda3", "anaconda3", "miniforge3" })
            {
                string conda = Path.Combine(parent, name);
                yield return Path.Combine(conda, "python.exe");
                foreach (string env in Subdirectories(Path.Combine(conda, "envs")))
                    yield return Path.Combine(env, "python.exe");
            }
        foreach (string env in Subdirectories(Path.Combine(user, ".conda", "envs")))
            yield return Path.Combine(env, "python.exe");
        // Conda records nonstandard environment locations here.
        foreach (string env in ReadLines(Path.Combine(user, ".conda", "environments.txt")))
            yield return Path.Combine(env, "python.exe");
    }

    private static IEnumerable<string> RegistryPythons(RegistryKey hive)
    {
        var result = new List<string>();
        try
        {
            using (var python = hive.OpenSubKey(@"SOFTWARE\Python"))
            {
                if (python == null) return result;
                foreach (string vendorName in python.GetSubKeyNames())
                    using (var vendor = python.OpenSubKey(vendorName))
                        foreach (string tag in vendor.GetSubKeyNames())
                            using (var install = vendor.OpenSubKey(tag + @"\InstallPath"))
                                if (install != null)
                                {
                                    string exe = install.GetValue("ExecutablePath") as string;
                                    string dir = install.GetValue("") as string;
                                    if (!string.IsNullOrEmpty(exe)) result.Add(exe);
                                    else if (!string.IsNullOrEmpty(dir)) result.Add(Path.Combine(dir, "python.exe"));
                                }
            }
        }
        catch (Exception ex) { Log.AppendLine("Registry: " + ex.Message); }
        return result;
    }

    private static string[] Subdirectories(string dir)
    {
        try { return Directory.Exists(dir) ? Directory.GetDirectories(dir) : new string[0]; }
        catch (IOException) { return new string[0]; }
        catch (UnauthorizedAccessException) { return new string[0]; }
    }

    private static string[] ReadLines(string path)
    {
        try { return File.Exists(path) ? File.ReadAllLines(path).Where(s => !string.IsNullOrWhiteSpace(s)).ToArray() : new string[0]; }
        catch (IOException) { return new string[0]; }
        catch (UnauthorizedAccessException) { return new string[0]; }
    }

    private static bool RunProbe(string exe, string arguments, out string output)
    {
        output = "";
        try
        {
            var start = StartInfo(exe, arguments);
            start.EnvironmentVariables.Remove("PYTHONHOME");
            start.EnvironmentVariables.Remove("PYTHONPATH");
            start.EnvironmentVariables["PYTHONUTF8"] = "1";
            using (var process = Process.Start(start))
            {
                var stdout = process.StandardOutput.ReadToEndAsync();
                var stderr = process.StandardError.ReadToEndAsync();
                if (!process.WaitForExit(5000))
                {
                    process.Kill();
                    Log.AppendLine("Probe timed out: " + exe);
                    return false;
                }
                output = stdout.Result;
                if (process.ExitCode == 0) return true;
                Log.AppendLine("Probe failed: " + exe + "\n" + stderr.Result);
            }
        }
        catch (Exception ex) { Log.AppendLine("Probe failed: " + exe + ": " + ex.Message); }
        return false;
    }

    private static ProcessStartInfo StartInfo(string exe, string arguments)
    {
        return new ProcessStartInfo(exe, arguments) {
            WorkingDirectory = Root, UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
        };
    }

    // Windows argv quoting, including embedded quotes and trailing backslashes.
    private static string Quote(string value)
    {
        return "\"" + Regex.Replace(Regex.Replace(value, "(\\\\*)\"", "$1$1\\\""), "(\\\\+)$", "$1$1") + "\"";
    }

    private static void SaveLog()
    {
        try { if (LogPath != null) File.WriteAllText(LogPath, Log.ToString(), Encoding.UTF8); }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }
}
