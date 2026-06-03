import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from collections import deque
import math
import heapq
import re
import json
import sys
import os
import threading

from PIL import Image, ImageTk

# ==========================================
# 步驟一：AI 與語音引擎初始化 (升級 Google 雲端語音)
# ==========================================
from google import genai
from gtts import gTTS

# 填入您的 Google Gemini API Key 
client = genai.Client(api_key="AIzaSyAxDkIOX4d6Ve3pXPGAQtfT33NsKo4Gg7w")

def speak_text(text):
    """背景語音播放器 (專治 Mac 卡畫面與外星語)"""
    def play_audio():
        try:
            # 使用 Google 語音產生台灣中文 mp3
            tts = gTTS(text=text, lang='zh-tw')
            tts.save("mrt_speech.mp3")
            # 使用 Mac 內建的 afplay 來播放音檔
            os.system("afplay mrt_speech.mp3") 
        except Exception as e:
            print(f"語音播放失敗: {e}")
            
    # 開啟背景執行緒，讓它自己去講話，Tkinter 畫面繼續順暢運作
    threading.Thread(target=play_audio, daemon=True).start()

def speak_result(start_name, end_name, fare, path_str):
    """語音朗讀函式"""
    clean_start = start_name.split(" ", 1)[-1] if " " in start_name else start_name
    clean_end = end_name.split(" ", 1)[-1] if " " in end_name else end_name
    text = f"已為您規劃從{clean_start}到{clean_end}的路徑。總票價{fare}元。"
    
    if "->" in path_str and ("(" in path_str): 
        text += "此路徑可能包含轉乘，請留意車廂廣播。"
    
    speak_text(text)

# ==========================================
# 步驟二：核心資料結構 (動態地圖)
# ==========================================
AVG_DISTANCE_PER_SEGMENT = 1.3

def load_system_data(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        messagebox.showerror("錯誤", f"找不到資料檔：{filepath}")
        return {}
    except json.JSONDecodeError:
        messagebox.showerror("錯誤", f"資料檔格式錯誤：{filepath}")
        return {}

def save_system_data(data_dict, filepath):
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data_dict, f, ensure_ascii=False, indent=4)
        messagebox.showinfo("成功", f"設定檔已成功儲存至\n{filepath}")
    except Exception as e:
        messagebox.showerror("錯誤", f"儲存失敗: {e}")

class Station:
    def __init__(self, sid, name, coords, line_type, neighbors):
        self.sid = sid
        self.name = name
        self.display_name = f"{sid} {name}"
        self.coords = coords
        self.line_type = line_type
        self.neighbors = neighbors
        
    def update_coords(self, x, y):
        self.coords = (x, y)

class TransitSystem:
    def __init__(self, data):
        self.stations = {}
        if not data: return 
        for sid, info in data.items():
            self.stations[sid] = Station(
                sid=sid,
                name=info["name"],
                coords=info["coords"],
                line_type=info["line_type"],
                neighbors=info["neighbors"]
            )
            
    def get_station(self, sid):
        return self.stations.get(sid)

    def get_all_display_names(self):
        if not self.stations: return []
        return sorted([s.display_name for s in self.stations.values()])
        
    def get_sid_by_name(self, display_name):
        for sid, s in self.stations.items():
            if s.display_name == display_name:
                return sid
        return None

# ==========================================
# 步驟三：大語言模型 (LLM) 解析函式 (支援動態地圖)
# ==========================================
def get_stations_from_ai(user_text, system):
    """將使用者的自然語言轉換為當前系統的捷運站 ID"""
    try:
        station_info = ", ".join([f"名稱:{s.name}(代號:{s.sid})" for s in system.stations.values()])
        
        prompt = f"""
        你是一個智慧捷運站點解析器。請從使用者的句子中，推斷出他想從哪個捷運站出發，以及目的地是哪個捷運站。
        這是一個動態載入的地圖，以下是目前所有可用的站點列表：
        [{station_info}]
        
        如果使用者說的是地標，請運用你的知識幫忙轉換成上述列表中最接近的捷運站代號。
        請「絕對嚴格」只輸出 JSON 格式，不要包含任何其他文字或 Markdown 標記。
        格式範例：
        {{"start_id": "R16", "end_id": "O10"}}
        
        使用者輸入：「{user_text}」
        """
        
        response = client.models.generate_content(
            model='gemini-2.5-flash', 
            contents=prompt
        )
        result_text = response.text.strip()
        
        match = re.search(r'\{.*\}', result_text, re.DOTALL)
        if match:
            clean_json = match.group(0)
            station_data = json.loads(clean_json)
            return station_data.get("start_id"), station_data.get("end_id"), "成功"
        else:
            return None, None, f"AI 回傳格式錯誤: {result_text}"
            
    except Exception as e:
        return None, None, str(e)

# ==========================================
# 步驟四：演算法 (尋路與計價) - 加入防呆機制
# ==========================================
def find_shortest_path(system, start_id, end_id):
    if not system.get_station(start_id) or not system.get_station(end_id): return []
    queue = deque([[start_id]])
    visited = {start_id}
    while queue:
        path = queue.popleft()
        if path[-1] == end_id: return path
        curr_station = system.get_station(path[-1])
        
        if not curr_station: continue # 🛡️ 防呆：避免斷鏈
            
        for neighbor in curr_station.neighbors:
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(path + [neighbor])
    return []

def find_cheapest_path(system, start_id, end_id):
    start_station = system.get_station(start_id)
    if not start_station: return []
    
    pq = [(0, 1, [start_id], start_station.line_type)]
    min_costs = {(start_id, start_station.line_type): (0, 1)}

    while pq:
        curr_fare, num_stat, path, curr_line = heapq.heappop(pq)
        curr_id = path[-1]
        
        best = min_costs.get((curr_id, curr_line), (float('inf'), float('inf')))
        if curr_fare > best[0] or (curr_fare == best[0] and num_stat > best[1]): continue
        if curr_id == end_id: return path

        curr_station = system.get_station(curr_id)
        if not curr_station: continue # 🛡️ 防呆
            
        for neighbor_id in curr_station.neighbors:
            neighbor_station = system.get_station(neighbor_id)
            if not neighbor_station: continue # 🛡️ 防呆
                
            new_path = path + [neighbor_id]
            new_fare, _ = calculate_fare_details(system, new_path)
            neighbor_line = neighbor_station.line_type
            
            best_neigh = min_costs.get((neighbor_id, neighbor_line), (float('inf'), float('inf')))
            if new_fare < best_neigh[0] or (new_fare == best_neigh[0] and len(new_path) < best_neigh[1]):
                min_costs[(neighbor_id, neighbor_line)] = (new_fare, len(new_path))
                heapq.heappush(pq, (new_fare, len(new_path), new_path, neighbor_line))
    return []

def calculate_fare_details(system, path_ids):
    if not path_ids or len(path_ids) < 2: return 0, "無須搭乘"
    total_fare, details = 0, []
    segment = [path_ids[0]]
    curr_line = system.get_station(path_ids[0]).line_type

    for i in range(1, len(path_ids)):
        sid = path_ids[i]
        next_line = system.get_station(sid).line_type
        
        if next_line != curr_line:
            segment.append(sid)
            dist = (len(segment)-1) * AVG_DISTANCE_PER_SEGMENT
            fare = 20 + (math.ceil((dist-5)/2)*5) if dist>5 else 20
            max_f = 35 if "C" in curr_line or "LRT" in curr_line else 60
            fare = min(fare, max_f)
            total_fare += fare
            details.append(f"  - {curr_line} ({len(segment)-1}站: {system.get_station(segment[0]).display_name} -> {system.get_station(segment[-1]).display_name})\n    (預估 {dist:.2f} 公里, 票價 {fare} 元)")
            segment, curr_line = [sid], next_line
        else:
            segment.append(sid)
            
    if len(segment) > 1:
        dist = (len(segment)-1) * AVG_DISTANCE_PER_SEGMENT
        fare = 20 + (math.ceil((dist-5)/2)*5) if dist>5 else 20
        max_f = 35 if "C" in curr_line or "LRT" in curr_line else 60
        fare = min(fare, max_f)
        total_fare += fare
        details.append(f"  - {curr_line} ({len(segment)-1}站: {system.get_station(segment[0]).display_name} -> {system.get_station(segment[-1]).display_name})\n    (預估 {dist:.2f} 公里, 票價 {fare} 元)")
        
    return total_fare, "\n".join(details)

# ==========================================
# 步驟五：GUI 啟動流程與建置
# ==========================================
window = tk.Tk()
window.withdraw()  # 先隱藏主視窗

DATA_FILE = filedialog.askopenfilename(
    title="請選擇捷運站點資料檔 (.json) - 若建立新地圖請選空 {} 的 JSON", 
    filetypes=[("JSON files", "*.json")]
)
if not DATA_FILE:
    messagebox.showinfo("提示", "未選擇資料檔，程式結束。")
    sys.exit()

MAP_IMAGE_FILE = filedialog.askopenfilename(
    title="請選擇地圖圖片 (.jpg / .png)", 
    filetypes=[("Image files", "*.jpg *.png")]
)
if not MAP_IMAGE_FILE:
    messagebox.showinfo("提示", "未選擇地圖圖片，程式結束。")
    sys.exit()

system_data = load_system_data(DATA_FILE)
krt = TransitSystem(system_data)

window.deiconify()  
window.title(f"通用捷運 AI 語音路徑規劃系統 - {DATA_FILE.split('/')[-1]}")
window.geometry("1300x750")

main_pane = ttk.PanedWindow(window, orient="horizontal")
main_pane.pack(fill="both", expand=True, padx=5, pady=5)
map_frame = ttk.Frame(main_pane)
control_frame = ttk.Frame(main_pane, padding="15")
main_pane.add(map_frame, weight=3)
main_pane.add(control_frame, weight=1)

# --- 地圖 Canvas ---
map_canvas = tk.Canvas(map_frame, bg="#333", cursor="cross")
map_canvas.pack(fill="both", expand=True)

try:
    pil_img = Image.open(MAP_IMAGE_FILE)
    map_image = ImageTk.PhotoImage(pil_img)
    map_canvas.config(scrollregion=(0, 0, pil_img.width, pil_img.height))
    map_canvas.create_image(0, 0, image=map_image, anchor="nw")
except Exception as e:
    messagebox.showerror("錯誤", f"無法載入圖片: {e}")

# ==========================================
# 步驟六：事件綁定與控制面板邏輯
# ==========================================
# --- AI 區塊 ---
ai_frame = ttk.LabelFrame(control_frame, text="✨ 告訴 AI 你想去哪裡", padding="10")
ai_frame.pack(fill="x", pady=(0, 15))

ai_input_var = tk.StringVar()
ai_entry = ttk.Entry(ai_frame, textvariable=ai_input_var, font=("Arial", 11))
ai_entry.pack(side="left", expand=True, fill="x", padx=5)
ai_entry.insert(0, "例如：從高鐵站搭到駁二怎麼走？")

def clear_ai_placeholder(event):
    if "例如：" in ai_input_var.get():
        ai_entry.delete(0, tk.END)
ai_entry.bind("<FocusIn>", clear_ai_placeholder)

def handle_ai_path():
    user_text = ai_input_var.get()
    if not user_text or "例如：" in user_text:
        update_result_text("請輸入您想去哪裡！")
        return
        
    update_result_text("🤖 AI 正在努力分析您的需求，請稍候...")
    window.update() 
    
    start_id, end_id, status_msg = get_stations_from_ai(user_text, krt)
    
    if start_id and end_id:
        if krt.get_station(start_id) and krt.get_station(end_id):
            start_combo.set(krt.get_station(start_id).display_name)
            end_combo.set(krt.get_station(end_id).display_name)
            run_search("cheapest", title_prefix="🤖 AI 智慧推薦 (最省錢)")
        else:
            update_result_text(f"AI 找出的站點代碼不在地圖中：起點 {start_id} / 終點 {end_id}")
    else:
        err_msg = f"❌ AI 分析失敗！\n\n【系統錯誤報告】：\n{status_msg}\n\n請檢查 API Key 或網路。"
        update_result_text(err_msg)
        speak_text("對不起，AI 系統發生錯誤。")

ai_button = ttk.Button(ai_frame, text="🤖 AI 幫我找", command=handle_ai_path)
ai_button.pack(side="right", padx=5)
window.bind('<Return>', lambda event: handle_ai_path() if ai_entry.focus_get() == ai_entry else None)

ttk.Separator(control_frame, orient='horizontal').pack(fill='x', pady=5)

# --- 基礎變數與畫圖邏輯 ---
next_click_is_start = True
markers = {"start": None, "end": None, "debug": []}
drag_data = {"item": None, "x": 0, "y": 0}

def on_mouse_wheel(event):
    if sys.platform == "darwin":
        map_canvas.yview_scroll(int(-1 * event.delta), "units")
    else:
        map_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

map_canvas.bind_all("<MouseWheel>", on_mouse_wheel)

def draw_debug_dots(show):
    for tag in markers["debug"]: map_canvas.delete(tag)
    markers["debug"] = []
    if show:
        for station in krt.stations.values():
            sx, sy = station.coords
            tag_id = f"dot_{station.sid}"
            item = map_canvas.create_oval(sx-6, sy-6, sx+6, sy+6, fill="yellow", outline="black", width=2, tags=("draggable", tag_id))
            markers["debug"].append(item)

# --- 控制項與編輯模式 ---
editor_mode_var = tk.BooleanVar(value=False)

def toggle_editor_mode():
    is_edit = editor_mode_var.get()
    if is_edit:
        instruction_label.config(text="【編輯模式】\n左鍵雙擊：新增站點\n拖曳黃點：修改位置\n完成後按「儲存修改」", foreground="red")
        start_combo.config(state="disabled")
        end_combo.config(state="disabled")
        draw_debug_dots(True)
    else:
        instruction_label.config(text="【一般模式】\n左鍵單擊地圖：設定起/終點\n左鍵拖曳地圖：移動視角", foreground="blue")
        start_combo.config(state="readonly")
        end_combo.config(state="readonly")
        draw_debug_dots(False)

chk_edit = ttk.Checkbutton(control_frame, text="啟用地圖建造/編輯模式", variable=editor_mode_var, command=toggle_editor_mode)
chk_edit.pack(pady=5)

instruction_label = ttk.Label(control_frame, text="【一般模式】\n左鍵單擊地圖：設定起/終點\n左鍵拖曳地圖：移動視角", foreground="blue")
instruction_label.pack(pady=5)

display_names_list = krt.get_all_display_names()
start_combo = ttk.Combobox(control_frame, values=display_names_list, state="readonly", font=("Arial", 11))
start_combo.pack(fill="x", pady=2)
end_combo = ttk.Combobox(control_frame, values=display_names_list, state="readonly", font=("Arial", 11))
end_combo.pack(fill="x", pady=2)

btn_frame = ttk.Frame(control_frame)
btn_frame.pack(pady=10, fill="x")
ttk.Button(btn_frame, text="查詢最短路徑", command=lambda: run_search("shortest", "最短路徑 (站數)")).pack(side="left", expand=True, fill="x", padx=2)
ttk.Button(btn_frame, text="查詢最省錢", command=lambda: run_search("cheapest", "最省錢路徑 (票價)")).pack(side="right", expand=True, fill="x", padx=2)

# --- 儲存與結果顯示區 ---
def export_coordinates():
    new_data = {}
    for sid, station in krt.stations.items():
        new_data[sid] = {
            "name": station.name,
            "coords": [int(station.coords[0]), int(station.coords[1])],
            "line_type": station.line_type,
            "neighbors": station.neighbors
        }
    save_system_data(new_data, DATA_FILE)
    update_result_text("★★★ 座標與站點已更新並存檔至 JSON！★★★\n\n下次啟動程式就會套用新的設定了。")

export_btn = ttk.Button(control_frame, text="💾 儲存地圖修改至 JSON", command=export_coordinates)
export_btn.pack(fill="x", pady=5)

# 🛠️ 解決 Mac 夜間模式白底白字的問題：加上 fg="black"
result_text = tk.Text(control_frame, height=15, width=40, font=("Arial", 11), state="disabled", bg="#ffffff", fg="black", wrap="word")
result_text.pack(fill="both", expand=True, pady=10)

def update_result_text(text):
    result_text.config(state="normal")
    result_text.delete("1.0", tk.END)
    result_text.insert("1.0", text)
    result_text.config(state="disabled")

# --- 地圖編輯器邏輯 ---
def on_dot_press(event):
    if not editor_mode_var.get(): return
    item = map_canvas.find_closest(map_canvas.canvasx(event.x), map_canvas.canvasy(event.y))[0]
    if "draggable" in map_canvas.gettags(item):
        drag_data["item"] = item
        drag_data["x"] = map_canvas.canvasx(event.x)
        drag_data["y"] = map_canvas.canvasy(event.y)

def on_dot_motion(event):
    if not editor_mode_var.get() or not drag_data["item"]: return
    cur_x, cur_y = map_canvas.canvasx(event.x), map_canvas.canvasy(event.y)
    map_canvas.move(drag_data["item"], cur_x - drag_data["x"], cur_y - drag_data["y"])
    drag_data["x"], drag_data["y"] = cur_x, cur_y
    
    item = drag_data["item"]
    for tag in map_canvas.gettags(item):
        if tag.startswith("dot_"):
            sid = tag.split("_")[1]
            coords = map_canvas.coords(item)
            krt.get_station(sid).update_coords((coords[0] + coords[2]) / 2, (coords[1] + coords[3]) / 2)
            break

def on_dot_release(event):
    drag_data["item"] = None

map_canvas.tag_bind("draggable", "<ButtonPress-1>", on_dot_press)
map_canvas.tag_bind("draggable", "<B1-Motion>", on_dot_motion)
map_canvas.tag_bind("draggable", "<ButtonRelease-1>", on_dot_release)

# ==========================================
# 左鍵智能判定 (相容 Mac 觸控板拖曳與點擊)
# ==========================================
map_drag_state = {"x": 0, "y": 0, "is_dragging": False}

def on_map_press(event):
    if editor_mode_var.get() and drag_data.get("item"): return
    map_drag_state["x"] = event.x
    map_drag_state["y"] = event.y
    map_drag_state["is_dragging"] = False
    map_canvas.scan_mark(event.x, event.y)

def on_map_motion(event):
    if editor_mode_var.get() and drag_data.get("item"): return
    if abs(event.x - map_drag_state["x"]) > 5 or abs(event.y - map_drag_state["y"]) > 5:
        map_drag_state["is_dragging"] = True
        map_canvas.scan_dragto(event.x, event.y, gain=1)

def on_map_release(event):
    if map_drag_state["is_dragging"]: return
    if editor_mode_var.get(): return 
    
    global next_click_is_start
    cx, cy = map_canvas.canvasx(event.x), map_canvas.canvasy(event.y)
    
    closest_station, min_dist = None, float('inf')
    for station in krt.stations.values():
        sx, sy = station.coords
        dist = math.sqrt((cx-sx)**2 + (cy-sy)**2)
        if dist < min_dist:
            min_dist, closest_station = dist, station
            
    if not closest_station or min_dist > 80: return

    s_name = closest_station.display_name
    sx, sy = closest_station.coords

    if next_click_is_start:
        start_combo.set(s_name)
        if markers["start"]: map_canvas.delete(markers["start"])
        markers["start"] = map_canvas.create_oval(sx-10, sy-10, sx+10, sy+10, outline="#00FF00", width=4)
        next_click_is_start = False
    else:
        end_combo.set(s_name)
        if markers["end"]: map_canvas.delete(markers["end"])
        markers["end"] = map_canvas.create_oval(sx-10, sy-10, sx+10, sy+10, outline="#FF0000", width=4)
        next_click_is_start = True
        run_search("cheapest", "最省錢路徑 (票價)")

def on_map_double_click(event):
    if not editor_mode_var.get(): return
    cx, cy = map_canvas.canvasx(event.x), map_canvas.canvasy(event.y)

    sid = simpledialog.askstring("新增站點", "請輸入站點代號 (例如: BL01):")
    if not sid: return
    name = simpledialog.askstring("新增站點", f"請輸入 {sid} 的站名 (例如: 頂埔):")
    if not name: return
    line_type = simpledialog.askstring("新增站點", f"請輸入 {sid} 的路線類型 (例如: MRT):", initialvalue="MRT")
    if not line_type: return

    krt.stations[sid] = Station(sid=sid, name=name, coords=(cx, cy), line_type=line_type, neighbors=[])
    draw_debug_dots(True)
    
    display_names_list = krt.get_all_display_names()
    start_combo.config(values=display_names_list)
    end_combo.config(values=display_names_list)
    messagebox.showinfo("成功", f"已新增 {sid} {name}！\n(請記得點擊「💾 儲存地圖修改至 JSON」)")

map_canvas.bind("<ButtonPress-1>", on_map_press)
map_canvas.bind("<B1-Motion>", on_map_motion)
map_canvas.bind("<ButtonRelease-1>", on_map_release)
map_canvas.bind("<Double-Button-1>", on_map_double_click)

def run_search(mode, title_prefix="路徑結果"):
    try:
        s_id = krt.get_sid_by_name(start_combo.get())
        e_id = krt.get_sid_by_name(end_combo.get())
        if not s_id or not e_id: 
            update_result_text("錯誤：請選擇有效的起點和終點。")
            return
        if s_id == e_id:
            update_result_text("您已在目的地！")
            return
        
        path_ids = find_shortest_path(krt, s_id, e_id) if mode=="shortest" else find_cheapest_path(krt, s_id, e_id)
        if not path_ids:
            err = "找不到路徑！請檢查設定檔中是否有設定 Neighbors (相鄰站點)。"
            update_result_text(err)
            speak_text("對不起，找不到路徑")
            return

        total_fare, fare_details = calculate_fare_details(krt, path_ids)
        
        display_path_names = []
        if mode == "cheapest":
            for i, node_id in enumerate(path_ids):
                st = krt.get_station(node_id)
                if i > 0 and st.name == krt.get_station(path_ids[i-1]).name:
                    display_path_names[-1] += f" (轉乘 {st.line_type})"
                else:
                    display_path_names.append(f"[{st.line_type}] {st.name}")
        else:
            display_path_names = [f"[{krt.get_station(pid).line_type}] {krt.get_station(pid).name}" for pid in path_ids]
            
        path_str = " \n-> ".join(display_path_names)

        final_result = (
            f"--- {title_prefix} ---\n"
            f"起訖： {start_combo.get()} ➔ {end_combo.get()}\n"
            f"總票價： {total_fare} 元\n"
            f"總站數： {len(path_ids)} 站\n"
            f"----------------------------------------\n"
            f"票價詳情：\n{fare_details}\n"
            f"----------------------------------------\n"
            f"建議路徑：\n{path_str}"
        )
        
        update_result_text(final_result)
        window.update()
        speak_result(start_combo.get(), end_combo.get(), total_fare, path_str)
        
    except Exception as e:
        err_msg = f"❌ 執行時發生錯誤：\n{str(e)}\n\n請檢查終端機的詳細錯誤報告。"
        update_result_text(err_msg)
        print(f"詳細錯誤: {repr(e)}")

window.mainloop()