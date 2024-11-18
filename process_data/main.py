import os
import fisher
def gen_scp_txt():
    dirs_path = "/root/autodl-tmp/fisher_data/trans"
    dirs = os.listdir(dirs_path)
    for dir in dirs:
        root_path = os.path.join(dirs_path,dir)
        txts = os.listdir(root_path)
        for txt in txts:
            my_fisher = fisher.fisher(txt.split(".")[0])
            my_fisher.create_dialogue(root_path)
            
    
def main():
    # 将转为wav的音频全部切割为30s
    # 将30s的音频对应的标签存到text文本中
    test_dir = ""
    train_dir = ""
    gen_scp_txt()

if __name__ == "__main__":
    main()