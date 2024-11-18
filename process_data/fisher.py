import os
import subprocess


class fisher:
    def __init__(self,file_name):
        self.file_name = file_name
        self.speaker = ""
        self.start = 0
        self.end = 0
        self.content=""
        self.index = 0
        self.text=""
    
    def create_dialogue(self,dir):
        #txt文件所在的路径
        file_path = os.path.join(dir,self.file_name+".txt")
        with open(file_path,"r",encoding="utf-8") as file:
            for line in file:
                if line.startswith("#") or not line.strip():
                    continue
                
                # Extracting start_time, end_time, speaker, and content
                parts = line.split()
                start_time = float(parts[0])
                end_time = float(parts[1])
                speaker = parts[2][0]  # Extracting the first character after the space
                content = ' '.join(parts[3:])  # Joining the rest of the parts as content 

                if end_time-self.start>30:
                    self.gen_audio_text()
                    if end_time - start_time >30:
                        self.start = end_time
                        self.end = end_time
                        self.speaker = ""
                        self.content = ""             
                    else:
                        self.start = start_time
                        self.end = end_time
                        self.speaker = speaker
                        self.content = f"<speaker {speaker}>{content} "
                else:
                    self.end = end_time
                    if self.speaker == speaker:
                        self.content+=content+" "
                    else:
                        
                        #print(f"说话者发生了转变。由{self.speaker}转变为{speaker}")
                        self.speaker = speaker
                        new_content = f"<speaker {speaker}>{content} "
                        self.content+=new_content
            # 最后一个也要存            
            self.gen_audio_text()


    def gen_audio_text(self):
        
        final_src_name = f"{self.file_name}_{self.index:03d}"
        self.index+=1
        #print(f"content:{self.content}\nstart:{self.start}\nend:{self.end}\nscr_name:{final_src_name}")
        # 创建test_file
        

        txt_file = "/root/xishaojian/MooER/src/label.txt"
        scp_file ="/root/xishaojian/MooER/src/wav.scp"
        audio_dir = os.path.join("/root/autodl-tmp/fisher_data/split_aduio",self.file_name)

        # 创建audio_dir
        if not os.path.exists(audio_dir):
            os.makedirs(audio_dir)




        # 更新音频标签
        with open(txt_file,"a") as file:
            txt = f"{final_src_name} {self.content}"
            file.write(txt+"\n")
        # 更新音频文件路径
        with open(scp_file,"a") as file:
            scp = f"{final_src_name} {audio_dir}/{final_src_name}.wav"
            file.write(scp+'\n')

        # 生成音频文件
        input_file = os.path.join("/root/autodl-tmp/fisher_data/wav_audio",f"{self.file_name}.wav")
        
        command = [
        'ffmpeg',
        '-i', input_file,
        '-c', 'copy',
        '-ss', str(self.start),
        '-to', str(self.end),
        os.path.join(audio_dir, f'{final_src_name}.wav')
        ]

        try:
            subprocess.run(command, check=True)
            print(f"Segment {final_src_name} successfully created.")
        except subprocess.CalledProcessError as e:
            print(f"An error occurred while creating segment {final_src_name}: {e}")
        