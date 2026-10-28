// Qamyal: разведчик Skype для бизнеса. Подключается к запущенному клиенту через официальный
// Lync 2013 SDK и пишет в файл (JSON-строки): участников звонка и «начал/перестал говорить».
// Запуск: SkypeProbe.exe <файл.jsonl>   (работает, пока его не остановят)
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Threading;
using Microsoft.Lync.Model;
using Microsoft.Lync.Model.Conversation;
using Microsoft.Lync.Model.Conversation.AudioVideo;

class SkypeProbe
{
    static StreamWriter log;
    static readonly object gate = new object();
    static readonly HashSet<string> hooked = new HashSet<string>();

    static string Esc(string s) { return (s ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\n", " "); }

    static void Write(string kind, string fields)
    {
        double t = (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds;
        lock (gate)
        {
            log.WriteLine("{\"t\":" + t.ToString("F3", System.Globalization.CultureInfo.InvariantCulture) +
                          ",\"kind\":\"" + kind + "\"" + (fields.Length > 0 ? "," + fields : "") + "}");
            log.Flush();
        }
    }

    static string Name(Participant p)
    {
        try { return (string)p.Contact.GetContactInformation(ContactInformationType.DisplayName); }
        catch { return ""; }
    }

    static string Uri(Participant p) { try { return p.Contact.Uri; } catch { return ""; } }

    static void Hook(Conversation c, Participant p)
    {
        string key = c.GetHashCode() + "|" + Uri(p);
        lock (gate) { if (!hooked.Add(key)) return; }
        string who = "\"name\":\"" + Esc(Name(p)) + "\",\"uri\":\"" + Esc(Uri(p)) + "\",\"self\":" + (p.IsSelf ? "true" : "false");
        Write("participant", who);
        try
        {
            var av = (AVModality)p.Modalities[ModalityTypes.AudioVideo];
            av.IsContributingChanged += (s, e) => Write(e.IsContributing ? "speak_start" : "speak_end", who);
            av.ModalityStateChanged += (s, e) => Write("av_state", who + ",\"state\":\"" + e.NewState + "\"");
        }
        catch (Exception ex) { Write("error", "\"where\":\"hook\",\"msg\":\"" + Esc(ex.Message) + "\"," + who); }
        try { p.IsMutedChanged += (s, e) => Write(e.IsMuted ? "muted" : "unmuted", who); } catch { }
    }

    static void HookConversation(Conversation c)
    {
        Write("conversation", "\"participants\":" + c.Participants.Count);
        foreach (var p in c.Participants) Hook(c, p);
        c.ParticipantAdded += (s, e) => Hook(c, e.Participant);
        c.ParticipantRemoved += (s, e) => Write("participant_left", "\"name\":\"" + Esc(Name(e.Participant)) + "\"");
        c.StateChanged += (s, e) => Write("conversation_state", "\"state\":\"" + e.NewState + "\"");
    }

    static int Main(string[] args)
    {
        string path = args.Length > 0 ? args[0] : "skype-probe.jsonl";
        log = new StreamWriter(path, true, new UTF8Encoding(false));
        LyncClient client;
        try { client = LyncClient.GetClient(); }
        catch (Exception ex) { Write("error", "\"where\":\"connect\",\"msg\":\"" + Esc(ex.Message) + "\""); return 2; }
        Write("connected", "\"state\":\"" + client.State + "\",\"conversations\":" + client.ConversationManager.Conversations.Count);
        foreach (var c in client.ConversationManager.Conversations) HookConversation(c);
        client.ConversationManager.ConversationAdded += (s, e) => HookConversation(e.Conversation);
        client.ConversationManager.ConversationRemoved += (s, e) => Write("conversation_removed", "");
        // события приходят сами; уровни громкости SDK в Skype 2016 всегда отдаёт нулями — их не пишем
        Thread.Sleep(Timeout.Infinite);
        return 0;
    }
}
