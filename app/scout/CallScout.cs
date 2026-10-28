// Qamyal: разведчик созвонов (Zoom, Teams, Телемост, МТС Линк, Битрикс24, Telegram, WhatsApp, звонки
// в браузере). Через специальные возможности Windows (UI Automation) раз в 2 секунды смотрит окна
// созвона и пишет в файл (JSON-строки), что в них изменилось: появились, пропали или переименовались
// элементы. По этим записям видно, как приложение показывает «кто сейчас говорит», — под каждое
// потом делается свой разбор, как для Skype для бизнеса. Только читает, ничего не нажимает.
// Заодно сам ищет в окнах подписи «кто говорит» («Анна, говорит», «Speaking: Anna», элемент с именем
// и состоянием «говорит») и пишет их во второй файл в том же виде, что журнал Skype: speak_start / speak_end.
// Запуск: CallScout.exe <разведка.jsonl> [<говорящие.jsonl>]   (работает, пока его не остановят)
// Сборка: csc /target:winexe /r:UIAutomationClient.dll /r:UIAutomationTypes.dll CallScout.cs
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Automation;

class CallScout
{
    // приложения созвонов (имя процесса без .exe, в нижнем регистре)
    static readonly string[] Apps = { "zoom", "ms-teams", "teams", "telemost", "yandex.telemost", "mts-link",
        "mtslink", "webinar", "bitrix24", "telegram", "whatsapp", "whatsapp.root", "skype", "discord", "jitsi meet" };
    static readonly string[] Browsers = { "chrome", "msedge", "browser", "firefox", "opera", "brave" };
    // вкладка браузера — созвон, если в заголовке есть одно из слов
    static readonly Regex CallTitle = new Regex(
        "телемост|telemost|zoom|teams|мтс линк|mts link|webinar|вебинар|битрикс|bitrix|meet|jitsi|видеозвон|звонок|созвон|конференц",
        RegexOptions.IgnoreCase);
    const int MaxNodes = 6000;
    const long MaxBytes = 60L * 1024 * 1024;

    // «Имя, говорит» / «Говорит: Имя» и то же по-английски
    static readonly Regex NameThenSpeak = new Regex(
        @"^(?<n>[^\n|]{2,60}?)[\s,:;—–-]+(сейчас\s+)?(говорит|выступает|is speaking|is talking|speaking|talking)(\s+сейчас|\s+now)?[.!]?$",
        RegexOptions.IgnoreCase);
    static readonly Regex SpeakThenName = new Regex(
        @"^(сейчас говорит|говорит|выступает|активный (докладчик|спикер)|speaking|talking|now speaking|current speaker|active speaker)[\s:—–-]+(?<n>[^\n|]{2,60})$",
        RegexOptions.IgnoreCase);
    static readonly Regex SpeakWord = new Regex(@"^(говорит|выступает|speaking|talking|is speaking|is talking)$", RegexOptions.IgnoreCase);
    static readonly Regex SelfMark = new Regex(@"\s*\((вы|you|я|me)\)\s*$", RegexOptions.IgnoreCase);

    static StreamWriter log, hints;
    static readonly Dictionary<string, HashSet<string>> talking = new Dictionary<string, HashSet<string>>();
    static readonly HashSet<string> introduced = new HashSet<string>();
    static long written;
    static readonly Dictionary<IntPtr, HashSet<string>> last = new Dictionary<IntPtr, HashSet<string>>();

    static string Esc(string s)
    {
        var b = new StringBuilder();
        foreach (char c in s ?? "")
        {
            if (c == '\\' || c == '"') b.Append('\\').Append(c);
            else if (c < ' ') b.Append(' ');
            else b.Append(c);
        }
        return b.ToString();
    }

    static double Now() { return (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds; }

    static void Write(string kind, string fields)
    {
        if (written > MaxBytes) return;
        string line = "{\"t\":" + Now().ToString("F3", System.Globalization.CultureInfo.InvariantCulture) +
                      ",\"kind\":\"" + kind + "\"" + (fields.Length > 0 ? "," + fields : "") + "}";
        written += Encoding.UTF8.GetByteCount(line) + 1;
        log.WriteLine(line);
        log.Flush();
    }

    static string Arr(IEnumerable<string> items)
    {
        return "[" + string.Join(",", items.Select(x => "\"" + Esc(x) + "\"")) + "]";
    }

    // один элемент окна → строка-подпись: тип | id | класс | имя | состояние | подсказка
    static string Sig(AutomationElement e)
    {
        try
        {
            var c = e.Cached;
            return c.ControlType.ProgrammaticName.Replace("ControlType.", "") + "|" + c.AutomationId + "|" +
                   c.ClassName + "|" + c.Name + "|" + c.ItemStatus + "|" + c.HelpText;
        }
        catch { return ""; }
    }

    // кто говорит в этом окне по подписям элементов (подпись — «тип|id|класс|имя|состояние|подсказка»)
    static HashSet<string> Speakers(HashSet<string> snap)
    {
        var who = new HashSet<string>();
        foreach (var s in snap)
        {
            var f = s.Split('|');
            if (f.Length < 6) continue;
            string name = f[3].Trim(), status = f[4].Trim(), help = f[5].Trim();
            Match m;
            if ((m = NameThenSpeak.Match(name)).Success || (m = SpeakThenName.Match(name)).Success)
                who.Add(m.Groups["n"].Value.Trim());
            else if (name.Length >= 2 && name.Length <= 60 && (SpeakWord.IsMatch(status) || SpeakWord.IsMatch(help)))
                who.Add(name);
        }
        return who;
    }

    static void Hint(string kind, string proc, string name)
    {
        if (hints == null) return;
        bool self = SelfMark.IsMatch(name);
        string clean = SelfMark.Replace(name, "").Trim();
        string uri = proc + ":" + clean.ToLowerInvariant();
        string t = Now().ToString("F3", System.Globalization.CultureInfo.InvariantCulture);
        string common = ",\"uri\":\"" + Esc(uri) + "\",\"name\":\"" + Esc(clean) + "\",\"self\":" + (self ? "true" : "false") +
                        ",\"src\":\"" + Esc(proc) + "\"}";
        if (introduced.Add(uri)) hints.WriteLine("{\"t\":" + t + ",\"kind\":\"participant\"" + common);
        hints.WriteLine("{\"t\":" + t + ",\"kind\":\"" + kind + "\"" + common);
        hints.Flush();
    }

    static void TrackSpeakers(string proc, HashSet<string> snap)
    {
        var now = Speakers(snap);
        HashSet<string> before;
        if (!talking.TryGetValue(proc, out before)) before = new HashSet<string>();
        foreach (var n in now.Where(x => !before.Contains(x))) Hint("speak_start", proc, n);
        foreach (var n in before.Where(x => !now.Contains(x))) Hint("speak_end", proc, n);
        talking[proc] = now;
    }

    static HashSet<string> Snapshot(AutomationElement win)
    {
        var req = new CacheRequest { TreeScope = TreeScope.Element };
        req.Add(AutomationElement.ControlTypeProperty);
        req.Add(AutomationElement.AutomationIdProperty);
        req.Add(AutomationElement.ClassNameProperty);
        req.Add(AutomationElement.NameProperty);
        req.Add(AutomationElement.ItemStatusProperty);
        req.Add(AutomationElement.HelpTextProperty);
        var set = new HashSet<string>();
        using (req.Activate())
        {
            var all = win.FindAll(TreeScope.Descendants, Condition.TrueCondition);
            int n = 0;
            foreach (AutomationElement e in all)
            {
                if (++n > MaxNodes) break;
                string s = Sig(e);
                if (s.Length > 0) set.Add(s.Length > 400 ? s.Substring(0, 400) : s);
            }
        }
        return set;
    }

    static List<Tuple<AutomationElement, string, string>> CallWindows(bool listAll)
    {
        var found = new List<Tuple<AutomationElement, string, string>>();
        var seen = new List<string>();
        foreach (AutomationElement w in AutomationElement.RootElement.FindAll(TreeScope.Children, Condition.TrueCondition))
        {
            string proc = "", title = "";
            try
            {
                title = w.Current.Name ?? "";
                proc = Process.GetProcessById(w.Current.ProcessId).ProcessName.ToLowerInvariant();
            }
            catch { continue; }
            if (title.Length == 0) continue;
            bool call = Apps.Contains(proc) || (Browsers.Contains(proc) && CallTitle.IsMatch(title));
            if (listAll) seen.Add(proc + " — " + title);
            if (call) found.Add(Tuple.Create(w, proc, title));
        }
        if (listAll) Write("windows", "\"list\":" + Arr(seen));
        return found;
    }

    static int Main(string[] args)
    {
        string path = args.Length > 0 ? args[0] : "call-scout.jsonl";
        log = new StreamWriter(path, true, new UTF8Encoding(false));
        if (args.Length > 1) hints = new StreamWriter(args[1], true, new UTF8Encoding(false));
        Write("started", "");
        int tick = 0;
        while (true)
        {
            try
            {
                // раз в минуту — список всех окон: так видно и приложения, которых нет в списке
                foreach (var w in CallWindows(tick % 30 == 0))
                {
                    IntPtr h;
                    try { h = new IntPtr(w.Item1.Current.NativeWindowHandle); } catch { continue; }
                    HashSet<string> now;
                    try { now = Snapshot(w.Item1); } catch (Exception ex) { Write("error", "\"msg\":\"" + Esc(ex.Message) + "\""); continue; }
                    string who = "\"proc\":\"" + Esc(w.Item2) + "\",\"title\":\"" + Esc(w.Item3) + "\"";
                    HashSet<string> before;
                    if (!last.TryGetValue(h, out before))
                        Write("snap", who + ",\"items\":" + Arr(now));
                    else
                    {
                        var add = now.Where(x => !before.Contains(x)).ToList();
                        var del = before.Where(x => !now.Contains(x)).ToList();
                        if (add.Count + del.Count > 0)
                            Write("diff", who + ",\"add\":" + Arr(add) + ",\"del\":" + Arr(del));
                    }
                    last[h] = now;
                    TrackSpeakers(w.Item2, now);
                }
            }
            catch (Exception ex) { Write("error", "\"msg\":\"" + Esc(ex.Message) + "\""); }
            tick++;
            Thread.Sleep(2000);
        }
    }
}
